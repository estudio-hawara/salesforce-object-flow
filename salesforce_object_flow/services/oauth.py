"""Salesforce OAuth 2.0 PKCE primitives and token-endpoint calls.

Pure stateless functions. No threading, no UI, no global state. Tests can pass
in an ``httpx.Client`` configured with ``httpx.MockTransport`` and a custom
``_rand`` to make PKCE generation deterministic.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx

from salesforce_object_flow.services.errors import CodedError, ErrorCode

log = logging.getLogger(__name__)

DEFAULT_SCOPES: tuple[str, ...] = ("api", "refresh_token", "offline_access")

# Salesforce's External Client App rejects ``http://127.0.0.1`` callbacks but
# accepts ``http://localhost``. We still bind the loopback server to the
# literal 127.0.0.1 interface (never 0.0.0.0) so the OS doesn't expose it on
# the network — the only difference is the URL the browser is told to redirect
# to, which the OS resolves back to the loopback interface.
CALLBACK_BIND_HOST = "127.0.0.1"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

_AUTHORIZE_PATH = "/services/oauth2/authorize"
_TOKEN_PATH = "/services/oauth2/token"
_REVOKE_PATH = "/services/oauth2/revoke"


class OAuthError(CodedError):
    """Salesforce OAuth endpoint returned an error envelope."""

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        *,
        code: ErrorCode | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, params=params)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class PKCEChallenge:
    """One PKCE handshake's verifier/challenge/state triple."""

    verifier: str
    challenge: str
    state: str
    method: str = "S256"


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """Decoded OAuth token response from Salesforce."""

    access_token: str
    instance_url: str
    refresh_token: str | None = None
    issued_at: str | None = None
    signature: str | None = None
    id_token: str | None = None
    scope: str | None = None
    token_type: str | None = None


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce(*, _rand: Callable[[int], bytes] = secrets.token_bytes) -> PKCEChallenge:
    """Build a fresh PKCE challenge.

    The ``_rand`` seam exists for tests; production should never pass it.
    """
    verifier = _b64url_nopad(_rand(32))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64url_nopad(digest)
    state = _b64url_nopad(_rand(32))
    return PKCEChallenge(verifier=verifier, challenge=challenge, state=state)


def build_authorize_url(
    my_domain_url: str,
    client_id: str,
    challenge: PKCEChallenge,
    scopes: Sequence[str] = DEFAULT_SCOPES,
    *,
    force_login: bool = False,
) -> str:
    """Compose the Salesforce ``/services/oauth2/authorize`` URL.

    ``force_login=True`` adds ``prompt=login`` so the user must re-enter
    credentials — used by the Re-auth flow, not by Add Org.
    """
    params: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", CALLBACK_URL),
        ("code_challenge", challenge.challenge),
        ("code_challenge_method", challenge.method),
        ("state", challenge.state),
        ("scope", " ".join(scopes)),
    ]
    if force_login:
        params.append(("prompt", "login"))
    base = my_domain_url.rstrip("/") + _AUTHORIZE_PATH
    return f"{base}?{urllib.parse.urlencode(params)}"


def _parse_token_bundle(payload: dict[str, Any]) -> TokenBundle:
    try:
        access_token = str(payload["access_token"])
        instance_url = str(payload["instance_url"])
    except KeyError as exc:
        raise OAuthError(
            f"Token response missing required field: {exc.args[0]}",
            code=ErrorCode.TOKEN_RESPONSE_INVALID,
            params={"field": str(exc.args[0])},
        ) from exc
    raw_refresh = payload.get("refresh_token")
    return TokenBundle(
        access_token=access_token,
        instance_url=instance_url,
        refresh_token=str(raw_refresh) if raw_refresh is not None else None,
        issued_at=_opt_str(payload.get("issued_at")),
        signature=_opt_str(payload.get("signature")),
        id_token=_opt_str(payload.get("id_token")),
        scope=_opt_str(payload.get("scope")),
        token_type=_opt_str(payload.get("token_type")),
    )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


_MAX_TOKEN_REDIRECTS = 3


def _post_token(client: httpx.Client, my_domain_url: str, body: dict[str, str]) -> TokenBundle:
    url = my_domain_url.rstrip("/") + _TOKEN_PATH
    response = _post_token_following_redirects(client, url, body)

    payload: Any
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400 or not isinstance(payload, dict):
        message, code = _extract_error(payload, response.status_code)
        raise OAuthError(message, error_code=code)
    return _parse_token_bundle(cast(dict[str, Any], payload))


def _post_token_following_redirects(
    client: httpx.Client, url: str, body: dict[str, str]
) -> httpx.Response:
    """POST to *url*; if Salesforce redirects (e.g. ``lightning.force.com`` →
    ``my.salesforce.com``), re-POST to the new location preserving the body.

    Browsers convert POST → GET on 302/303 by default, which would silently
    lose the OAuth body — we follow manually to keep the method intact.
    """
    current: str = url
    for _ in range(_MAX_TOKEN_REDIRECTS + 1):
        response = client.post(
            current,
            data=body,
            headers={"Accept": "application/json"},
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        current = str(httpx.URL(current).join(location))
        log.debug(
            "Following %s redirect from token endpoint to %s",
            response.status_code,
            current,
        )
    raise OAuthError(
        "Too many redirects from the Salesforce token endpoint.",
        code=ErrorCode.OAUTH_TOO_MANY_REDIRECTS,
    )


def _extract_error(payload: Any, status_code: int) -> tuple[str, str | None]:
    if isinstance(payload, dict):
        envelope = cast(dict[str, Any], payload)
        error = envelope.get("error")
        description = envelope.get("error_description")
        code = str(error) if error is not None else None
        if description is not None:
            return f"{code or 'oauth_error'}: {description}", code
        if code is not None:
            return code, code
    return f"OAuth request failed with HTTP {status_code}", None


def exchange_code(
    client: httpx.Client,
    my_domain_url: str,
    client_id: str,
    code: str,
    verifier: str,
) -> TokenBundle:
    """Trade an authorization code for an access + refresh token pair."""
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": verifier,
        "redirect_uri": CALLBACK_URL,
    }
    return _post_token(client, my_domain_url, body)


def refresh_access_token(
    client: httpx.Client,
    my_domain_url: str,
    client_id: str,
    refresh_token: str,
) -> TokenBundle:
    """Exchange a refresh token for a fresh access token.

    Salesforce may or may not include a new ``refresh_token`` in the response;
    callers must keep their existing one when ``bundle.refresh_token is None``.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    return _post_token(client, my_domain_url, body)


def revoke(client: httpx.Client, my_domain_url: str, token: str) -> None:
    """Best-effort token revocation. Failures are logged, not raised."""
    url = my_domain_url.rstrip("/") + _REVOKE_PATH
    try:
        response = client.post(url, data={"token": token})
    except httpx.RequestError as exc:
        log.warning("Revoke request failed: %s", exc)
        return
    if response.status_code >= 400:
        log.warning("Revoke returned HTTP %s: %s", response.status_code, response.text[:200])
