"""Authenticated Salesforce REST client.

Thin wrapper around an ``httpx.Client``. Injects the bearer token, retries
once on 401 by calling a caller-supplied refresh callback, and surfaces
parsed JSON for the orchestrator's ``test_connection`` to format.

The wrapper is intentionally minimal — just enough for the upcoming Composite
API form to grow on top. It does not import ``services.connections`` to keep
the dependency graph one-way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import httpx

from salesforce_object_flow.core.credentials import OrgCredentials
from salesforce_object_flow.services.errors import CodedError, ErrorCode

log = logging.getLogger(__name__)


class ApiError(CodedError):
    """Generic Salesforce REST failure surfaced to the user."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: ErrorCode | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, params=params)
        self.status_code = status_code


RefreshFn = Callable[[], OrgCredentials]
OnTokenRefresh = Callable[[OrgCredentials], None]


class SalesforceClient:
    """Per-org HTTP client. Cheap to construct; reuses a shared httpx.Client."""

    def __init__(
        self,
        creds: OrgCredentials,
        api_version: str,
        client: httpx.Client,
        *,
        refresh_fn: RefreshFn | None = None,
        on_token_refresh: OnTokenRefresh | None = None,
    ) -> None:
        self._creds = creds
        self._api_version = api_version
        self._client = client
        self._refresh_fn = refresh_fn
        self._on_token_refresh = on_token_refresh

    @property
    def api_version(self) -> str:
        return self._api_version

    @property
    def credentials(self) -> OrgCredentials:
        return self._creds

    def get(self, path: str) -> Any:
        return self._request("GET", path).json()

    def post(self, path: str, json: object) -> Any:
        return self._request("POST", path, json=json).json()

    def patch(self, path: str, json: object) -> Any:
        response = self._request("PATCH", path, json=json)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def put(self, path: str, json: object) -> Any:
        response = self._request("PUT", path, json=json)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def delete(self, path: str) -> Any:
        response = self._request("DELETE", path)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def request(self, method: str, path: str, *, json: object = None) -> Any:
        """Dispatch by method name. Used by serial executor to keep verb-agnostic code."""
        response = self._request(method.upper(), path, json=json)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def limits(self) -> dict[str, Any]:
        payload = self.get(f"/services/data/{self._api_version}/limits")
        if not isinstance(payload, dict):
            raise ApiError(
                "Unexpected /limits response shape", code=ErrorCode.API_UNEXPECTED_RESPONSE
            )
        return cast(dict[str, Any], payload)

    def _request(self, method: str, path: str, *, json: object = None) -> httpx.Response:
        url = self._creds.instance_url.rstrip("/") + path
        response = self._send(method, url, json=json)
        if response.status_code != 401 or self._refresh_fn is None:
            self._raise_for_status(response)
            return response

        log.debug("Got 401 on %s %s; attempting token refresh", method, path)
        try:
            new_creds = self._refresh_fn()
        except Exception as exc:
            raise ApiError(
                f"Session expired and refresh failed: {exc}",
                status_code=401,
                code=ErrorCode.SESSION_EXPIRED,
            ) from exc

        self._creds = new_creds
        if self._on_token_refresh is not None:
            self._on_token_refresh(new_creds)

        url = new_creds.instance_url.rstrip("/") + path
        retry = self._send(method, url, json=json)
        if retry.status_code == 401:
            raise ApiError(
                "Session expired and refresh failed",
                status_code=401,
                code=ErrorCode.SESSION_EXPIRED,
            )
        self._raise_for_status(retry)
        return retry

    def _send(self, method: str, url: str, *, json: object) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._creds.access_token}",
            "Accept": "application/json",
        }
        try:
            return self._client.request(method, url, headers=headers, json=json)
        except httpx.RequestError as exc:
            raise ApiError(
                f"HTTP request failed: {exc}",
                code=ErrorCode.HTTP_REQUEST_FAILED,
                params={"error": str(exc)},
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = self._format_error(response)
        raise ApiError(message, status_code=response.status_code)

    def _format_error(self, response: httpx.Response) -> str:
        try:
            payload: Any = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:200]}"
        if isinstance(payload, list) and payload:
            first = cast(dict[str, Any], payload[0]) if isinstance(payload[0], dict) else None
            if first is not None:
                code = first.get("errorCode") or first.get("error")
                msg = first.get("message") or first.get("error_description")
                if code or msg:
                    return f"{code or 'error'}: {msg or 'unknown error'}"
        if isinstance(payload, dict):
            envelope = cast(dict[str, Any], payload)
            code = envelope.get("errorCode") or envelope.get("error")
            msg = envelope.get("message") or envelope.get("error_description")
            if code or msg:
                return f"{code or 'error'}: {msg or 'unknown error'}"
        return f"HTTP {response.status_code}: {response.text[:200]}"


def update_creds_after_refresh(
    old: OrgCredentials, refreshed_access_token: str, refreshed_refresh_token: str | None
) -> OrgCredentials:
    """Build a new ``OrgCredentials`` keeping the previous refresh token if absent."""
    return replace(
        old,
        access_token=refreshed_access_token,
        refresh_token=refreshed_refresh_token or old.refresh_token,
    )
