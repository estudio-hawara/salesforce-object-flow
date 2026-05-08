"""Structured error model for service-layer failures.

Each user-facing exception carries an ``ErrorCode`` plus a ``params`` dict.
The English ``__str__`` is kept (for logs and bug reports), but the UI
formats a localized message via :func:`salesforce_object_flow.i18n_errors.format_error`.

New raises should pass ``code=`` and ``params=`` so the toast layer can
translate; old call-sites without a code still work — :func:`format_error`
falls back to ``str(exc)`` (English) when no code is present.
"""

from __future__ import annotations

from enum import Enum, auto


class ErrorCode(Enum):
    # ----- Connections / OAuth orchestration
    ALIAS_ALREADY_EXISTS = auto()
    OAUTH_CANCELLED = auto()
    OAUTH_EXCHANGE_FAILED = auto()
    TOKEN_REFRESH_FAILED = auto()
    NO_REFRESH_TOKEN = auto()
    NO_STORED_CREDENTIALS = auto()
    UNKNOWN_ALIAS = auto()

    # ----- Composite templates
    TEMPLATE_SAVE_FAILED = auto()
    TEMPLATE_DELETE_FAILED = auto()

    # ----- File formats
    FORMAT_SAVE_FAILED = auto()
    FORMAT_DELETE_FAILED = auto()

    # ----- Composite execution
    CSV_UNREADABLE = auto()
    CSV_DECODE_ERROR = auto()
    AUTH_FAILED = auto()

    # ----- Loopback (PKCE callback server)
    PORT_IN_USE = auto()
    LOOPBACK_BIND_FAILED = auto()
    LOOPBACK_NOT_RUNNING = auto()
    OAUTH_TIMEOUT = auto()

    # ----- OAuth token endpoint
    TOKEN_RESPONSE_INVALID = auto()
    OAUTH_TOO_MANY_REDIRECTS = auto()

    # ----- Salesforce REST
    API_UNEXPECTED_RESPONSE = auto()
    SESSION_EXPIRED = auto()
    HTTP_REQUEST_FAILED = auto()


class CodedError(RuntimeError):
    """Mixin for service errors. ``code`` + ``params`` enable i18n at the toast boundary.

    Subclasses keep accepting a positional message for backwards compatibility with
    existing tests that match on the string. Pass ``code=...`` and ``params=...``
    on raises that are surfaced to the user.
    """

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.params: dict[str, object] = dict(params) if params else {}
