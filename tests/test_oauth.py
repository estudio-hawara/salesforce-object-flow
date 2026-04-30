"""Tests for ``services/oauth.py`` — PKCE primitives and token endpoints."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import httpx
import pytest

from salesforce_object_flow.services.oauth import (
    CALLBACK_URL,
    DEFAULT_SCOPES,
    OAuthError,
    build_authorize_url,
    exchange_code,
    generate_pkce,
    refresh_access_token,
    revoke,
)

MY_DOMAIN = "https://acme.my.salesforce.com"
CLIENT_ID = "3MVG9...test"


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_pkce_verifier_length_and_charset() -> None:
    challenge = generate_pkce()
    assert 43 <= len(challenge.verifier) <= 128
    # URL-safe base64 (no padding) charset.
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(challenge.verifier) <= allowed


def test_pkce_challenge_is_s256_of_verifier() -> None:
    challenge = generate_pkce()
    expected = _b64url_nopad(hashlib.sha256(challenge.verifier.encode("ascii")).digest())
    assert challenge.challenge == expected
    assert challenge.method == "S256"


def test_pkce_state_entropy() -> None:
    states = {generate_pkce().state for _ in range(100)}
    assert len(states) == 100


def test_authorize_url_contains_required_params() -> None:
    challenge = generate_pkce()
    url = build_authorize_url(MY_DOMAIN, CLIENT_ID, challenge)

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "acme.my.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [CALLBACK_URL]
    assert params["code_challenge"] == [challenge.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [challenge.state]
    assert params["scope"] == [" ".join(DEFAULT_SCOPES)]
    assert "prompt" not in params


def test_authorize_url_force_login_adds_prompt() -> None:
    challenge = generate_pkce()
    url = build_authorize_url(MY_DOMAIN, CLIENT_ID, challenge, force_login=True)
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["prompt"] == ["login"]


def test_authorize_url_strips_trailing_slash() -> None:
    challenge = generate_pkce()
    url = build_authorize_url(MY_DOMAIN + "/", CLIENT_ID, challenge)
    assert url.startswith(MY_DOMAIN + "/services/oauth2/authorize?")


def _mock_client(handler: httpx.MockTransport | object) -> httpx.Client:
    transport = handler if isinstance(handler, httpx.MockTransport) else None
    assert transport is not None
    return httpx.Client(transport=transport)


def test_exchange_code_request_body_and_response_parsing() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "instance_url": "https://acme.my.salesforce.com",
                "issued_at": "1700000000",
                "signature": "sig",
                "id_token": "idtok",
                "scope": "api refresh_token",
                "token_type": "Bearer",
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    bundle = exchange_code(client, MY_DOMAIN, CLIENT_ID, code="C", verifier="V")

    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.method == "POST"
    assert str(req.url) == MY_DOMAIN + "/services/oauth2/token"
    body = urllib.parse.parse_qs(req.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["C"]
    assert body["client_id"] == [CLIENT_ID]
    assert body["code_verifier"] == ["V"]
    assert body["redirect_uri"] == [CALLBACK_URL]

    assert bundle.access_token == "AT"
    assert bundle.refresh_token == "RT"
    assert bundle.instance_url == "https://acme.my.salesforce.com"
    assert bundle.issued_at == "1700000000"
    assert bundle.token_type == "Bearer"


def test_exchange_code_error_response_raises_oauth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "expired authorization code",
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    with pytest.raises(OAuthError) as exc:
        exchange_code(client, MY_DOMAIN, CLIENT_ID, code="C", verifier="V")

    assert exc.value.error_code == "invalid_grant"
    assert "expired authorization code" in str(exc.value)


def test_exchange_code_handles_non_json_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>oops</html>")

    client = _mock_client(httpx.MockTransport(handler))
    with pytest.raises(OAuthError) as exc:
        exchange_code(client, MY_DOMAIN, CLIENT_ID, code="C", verifier="V")

    assert "HTTP 500" in str(exc.value)


def test_refresh_access_token_request_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "AT2",
                "instance_url": "https://acme.my.salesforce.com",
                "refresh_token": "RT2",
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    bundle = refresh_access_token(client, MY_DOMAIN, CLIENT_ID, refresh_token="OLD")

    body = urllib.parse.parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["OLD"]
    assert body["client_id"] == [CLIENT_ID]

    assert bundle.access_token == "AT2"
    assert bundle.refresh_token == "RT2"


def test_refresh_preserves_existing_when_response_omits_refresh_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "AT3",
                "instance_url": "https://acme.my.salesforce.com",
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    bundle = refresh_access_token(client, MY_DOMAIN, CLIENT_ID, refresh_token="OLD")

    assert bundle.refresh_token is None


def test_exchange_code_follows_redirect_preserving_post() -> None:
    """Salesforce 302s from lightning.force.com to my.salesforce.com on /token.

    httpx would convert POST→GET by default; we must follow manually keeping
    the body and method, otherwise the OAuth handshake silently fails.
    """
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if "lightning.force.com" in str(request.url):
            return httpx.Response(
                302,
                headers={"Location": "https://acme.my.salesforce.com/services/oauth2/token"},
            )
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "instance_url": "https://acme.my.salesforce.com",
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    bundle = exchange_code(
        client,
        "https://acme.lightning.force.com",
        CLIENT_ID,
        code="C",
        verifier="V",
    )

    assert bundle.access_token == "AT"
    methods = [m for m, _ in seen]
    assert methods == ["POST", "POST"], f"Expected redirect to be followed as POST; got {methods}"
    assert "lightning.force.com" in seen[0][1]
    assert "my.salesforce.com" in seen[1][1]


def test_exchange_code_caps_redirect_chain() -> None:
    """Don't loop forever if Salesforce keeps redirecting."""
    counter: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(1)
        return httpx.Response(
            302,
            headers={"Location": f"https://acme.my.salesforce.com/r{len(counter)}/token"},
        )

    client = _mock_client(httpx.MockTransport(handler))
    with pytest.raises(OAuthError, match="redirects"):
        exchange_code(client, "https://acme.lightning.force.com", CLIENT_ID, code="C", verifier="V")


def test_revoke_swallows_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="already revoked")

    client = _mock_client(httpx.MockTransport(handler))
    revoke(client, MY_DOMAIN, token="AT")  # must not raise


def test_revoke_swallows_network_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = _mock_client(httpx.MockTransport(handler))
    revoke(client, MY_DOMAIN, token="AT")  # must not raise
