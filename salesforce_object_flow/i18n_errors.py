"""Translate service-layer errors into localized toast messages.

Lives in its own module (not ``services/errors.py``) because the services
layer must stay UI-agnostic — only the toast boundary in ``pages/*`` should
import this.
"""

from __future__ import annotations

from collections.abc import Callable

from salesforce_object_flow.i18n import _
from salesforce_object_flow.services.errors import ErrorCode


def _alias_already_exists(p: dict[str, object]) -> str:
    return _("A connection with alias “{alias}” already exists.").format(**p)


def _oauth_cancelled(_p: dict[str, object]) -> str:
    return _("Cancelled.")


def _oauth_exchange_failed(p: dict[str, object]) -> str:
    return _("Could not exchange authorization code: {error}").format(**p)


def _token_refresh_failed(p: dict[str, object]) -> str:
    return _("Could not refresh token: {error}").format(**p)


def _no_refresh_token(p: dict[str, object]) -> str:
    return _("No refresh token stored for “{alias}”. Re-authenticate to continue.").format(**p)


def _no_stored_credentials(p: dict[str, object]) -> str:
    return _("No stored credentials for “{alias}”. Re-authenticate to continue.").format(**p)


def _unknown_alias(p: dict[str, object]) -> str:
    return _("No connection with alias “{alias}” is registered.").format(**p)


def _template_save_failed(p: dict[str, object]) -> str:
    return _("Could not save template: {error}").format(**p)


def _template_delete_failed(p: dict[str, object]) -> str:
    return _("Could not delete template: {error}").format(**p)


def _format_save_failed(p: dict[str, object]) -> str:
    return _("Could not save format: {error}").format(**p)


def _format_delete_failed(p: dict[str, object]) -> str:
    return _("Could not delete format: {error}").format(**p)


def _csv_unreadable(p: dict[str, object]) -> str:
    return _("CSV unreadable: {error}").format(**p)


def _csv_decode_error(p: dict[str, object]) -> str:
    return _("Could not decode {path} as {encoding}: {error}").format(**p)


def _auth_failed(_p: dict[str, object]) -> str:
    return _("Authentication failed. Re-authenticate the connection and try again.")


def _port_in_use(p: dict[str, object]) -> str:
    return _(
        "Port {port} is already in use. Close any other process bound to it "
        "and try again."
    ).format(**p)


def _loopback_bind_failed(p: dict[str, object]) -> str:
    return _("Could not bind loopback server: {error}").format(**p)


def _loopback_not_running(_p: dict[str, object]) -> str:
    return _("Loopback server is not running.")


def _oauth_timeout(_p: dict[str, object]) -> str:
    return _("Authorization timed out before the callback arrived.")


def _token_response_invalid(p: dict[str, object]) -> str:
    return _("Token response missing required field: {field}").format(**p)


def _oauth_too_many_redirects(_p: dict[str, object]) -> str:
    return _("Too many redirects from the Salesforce token endpoint.")


def _api_unexpected_response(_p: dict[str, object]) -> str:
    return _("Unexpected response from Salesforce.")


def _session_expired(_p: dict[str, object]) -> str:
    return _("Session expired and refresh failed. Re-authenticate the connection.")


def _http_request_failed(p: dict[str, object]) -> str:
    return _("HTTP request failed: {error}").format(**p)


_TEMPLATES: dict[ErrorCode, Callable[[dict[str, object]], str]] = {
    ErrorCode.ALIAS_ALREADY_EXISTS: _alias_already_exists,
    ErrorCode.OAUTH_CANCELLED: _oauth_cancelled,
    ErrorCode.OAUTH_EXCHANGE_FAILED: _oauth_exchange_failed,
    ErrorCode.TOKEN_REFRESH_FAILED: _token_refresh_failed,
    ErrorCode.NO_REFRESH_TOKEN: _no_refresh_token,
    ErrorCode.NO_STORED_CREDENTIALS: _no_stored_credentials,
    ErrorCode.UNKNOWN_ALIAS: _unknown_alias,
    ErrorCode.TEMPLATE_SAVE_FAILED: _template_save_failed,
    ErrorCode.TEMPLATE_DELETE_FAILED: _template_delete_failed,
    ErrorCode.FORMAT_SAVE_FAILED: _format_save_failed,
    ErrorCode.FORMAT_DELETE_FAILED: _format_delete_failed,
    ErrorCode.CSV_UNREADABLE: _csv_unreadable,
    ErrorCode.CSV_DECODE_ERROR: _csv_decode_error,
    ErrorCode.AUTH_FAILED: _auth_failed,
    ErrorCode.PORT_IN_USE: _port_in_use,
    ErrorCode.LOOPBACK_BIND_FAILED: _loopback_bind_failed,
    ErrorCode.LOOPBACK_NOT_RUNNING: _loopback_not_running,
    ErrorCode.OAUTH_TIMEOUT: _oauth_timeout,
    ErrorCode.TOKEN_RESPONSE_INVALID: _token_response_invalid,
    ErrorCode.OAUTH_TOO_MANY_REDIRECTS: _oauth_too_many_redirects,
    ErrorCode.API_UNEXPECTED_RESPONSE: _api_unexpected_response,
    ErrorCode.SESSION_EXPIRED: _session_expired,
    ErrorCode.HTTP_REQUEST_FAILED: _http_request_failed,
}


def format_error(exc: BaseException) -> str:
    """Return a translated, user-facing message for ``exc``.

    Looks at ``exc.code`` first and renders the matching template against
    ``exc.params``. Falls back to ``str(exc)`` (the English message used for
    logs) when no code is present.
    """
    code: ErrorCode | None = getattr(exc, "code", None)
    if code is None:
        return str(exc)
    params: dict[str, object] = getattr(exc, "params", {}) or {}
    formatter = _TEMPLATES.get(code)
    if formatter is None:
        return str(exc)
    return formatter(params)
