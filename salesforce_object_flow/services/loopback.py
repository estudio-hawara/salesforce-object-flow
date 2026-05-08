"""One-shot HTTP loopback listener for the OAuth PKCE callback.

Stdlib only. Lifecycle::

    server = LoopbackServer(expected_state="abc...")
    server.start()                       # binds, returns immediately
    try:
        result = server.wait(timeout=300)
    finally:
        server.stop()                    # idempotent

The server must run on a daemon thread; the orchestrator polls
``server.wait(timeout=1)`` from a worker thread between cancellation checks.
The GTK main loop never touches this module.
"""

from __future__ import annotations

import errno
import http.server
import logging
import socketserver
import threading
import urllib.parse
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Final

from salesforce_object_flow.services.errors import CodedError, ErrorCode
from salesforce_object_flow.services.oauth import CALLBACK_PATH

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: str
    state: str


class LoopbackError(CodedError):
    """Bind failure, state mismatch, provider error, or timeout."""


_DEFAULT_SUCCESS_HTML: Final[str] = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Salesforce Object Flow — connected</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; background: #f4f6fa;
         color: #1f2937; margin: 0; padding: 4rem 1rem; }
  .card { max-width: 28rem; margin: 0 auto; background: #fff; border-radius: 12px;
          box-shadow: 0 6px 24px rgba(15, 23, 42, 0.08); padding: 2rem; }
  h1 { font-size: 1.4rem; margin: 0 0 0.5rem; }
  p { line-height: 1.5; color: #4b5563; margin: 0; }
</style>
</head>
<body>
  <div class="card">
    <h1>You can close this tab.</h1>
    <p>Salesforce Object Flow has received your authorization.</p>
  </div>
</body>
</html>
"""

_DEFAULT_ERROR_HTML_TEMPLATE: Final[str] = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Salesforce Object Flow — error</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; background: #fff7f5;
         color: #7f1d1d; margin: 0; padding: 4rem 1rem; }
  .card { max-width: 28rem; margin: 0 auto; background: #fff; border-radius: 12px;
          box-shadow: 0 6px 24px rgba(127, 29, 29, 0.10);
          border-left: 4px solid #dc2626; padding: 2rem; }
  h1 { font-size: 1.4rem; margin: 0 0 0.5rem; color: #991b1b; }
  p { line-height: 1.5; color: #1f2937; margin: 0; }
</style>
</head>
<body>
  <div class="card">
    <h1>Authorization failed</h1>
    <p>{{MESSAGE}}</p>
  </div>
</body>
</html>
"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handler injected with shared state via the server instance."""

    # Suppress the default access log spam.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        server = self.server
        assert isinstance(server, _LoopbackHTTPServer)

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        error = _first(params.get("error"))
        if error is not None:
            description = _first(params.get("error_description")) or error
            self._respond_error(server, description)
            server.set_error(LoopbackError(f"{error}: {description}"))
            return

        code = _first(params.get("code"))
        state = _first(params.get("state"))
        if code is None or state is None:
            self._respond_error(server, "Callback missing 'code' or 'state' parameter.")
            server.set_error(LoopbackError("Callback missing 'code' or 'state' parameter."))
            return

        if state != server.expected_state:
            self._respond_error(
                server,
                "State mismatch — likely a stale browser tab. Please retry.",
            )
            server.set_error(
                LoopbackError(
                    "Authorization state mismatch — likely a stale browser tab. Please retry."
                )
            )
            return

        self._respond_success(server)
        server.set_result(CallbackResult(code=code, state=state))

    def _respond_success(self, server: _LoopbackHTTPServer) -> None:
        body = server.success_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_error(self, server: _LoopbackHTTPServer, message: str) -> None:
        body = server.error_html_template.replace("{{MESSAGE}}", _html_escape(message)).encode(
            "utf-8"
        )
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


class _LoopbackHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server with a shared one-shot Future and config."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        expected_state: str,
        success_html: str,
        error_html_template: str,
    ) -> None:
        super().__init__(server_address, _CallbackHandler)
        self.expected_state = expected_state
        self.success_html = success_html
        self.error_html_template = error_html_template
        self._future: Future[CallbackResult] = Future()
        self._lock = threading.Lock()

    def set_result(self, result: CallbackResult) -> None:
        with self._lock:
            if not self._future.done():
                self._future.set_result(result)

    def set_error(self, error: BaseException) -> None:
        with self._lock:
            if not self._future.done():
                self._future.set_exception(error)

    @property
    def future(self) -> Future[CallbackResult]:
        return self._future


class LoopbackServer:
    """Public facade. Owns the HTTP server, the serving thread, and the future."""

    def __init__(
        self,
        expected_state: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        success_html: str | None = None,
        error_html_template: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._expected_state = expected_state
        self._success_html = success_html or _DEFAULT_SUCCESS_HTML
        self._error_html_template = error_html_template or _DEFAULT_ERROR_HTML_TEMPLATE
        self._server: _LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def port(self) -> int:
        """Port actually bound. Useful when constructed with ``port=0``."""
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Bind and start serving. Raises ``LoopbackError`` if the port is taken."""
        try:
            server = _LoopbackHTTPServer(
                (self._host, self._port),
                expected_state=self._expected_state,
                success_html=self._success_html,
                error_html_template=self._error_html_template,
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise LoopbackError(
                    f"Port {self._port} is already in use. Close any other "
                    f"Salesforce Object Flow instance and try again.",
                    code=ErrorCode.PORT_IN_USE,
                    params={"port": self._port},
                ) from exc
            raise LoopbackError(
                f"Could not bind loopback server: {exc}",
                code=ErrorCode.LOOPBACK_BIND_FAILED,
                params={"error": str(exc)},
            ) from exc

        self._server = server
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"oauth-loopback-{self._port}",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def wait(self, timeout: float) -> CallbackResult:
        """Block up to *timeout* seconds for a callback. Raises on error/timeout."""
        if self._server is None:
            raise LoopbackError(
                "Loopback server is not running.",
                code=ErrorCode.LOOPBACK_NOT_RUNNING,
            )
        try:
            return self._server.future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise LoopbackError(
                "Authorization timed out before the callback arrived.",
                code=ErrorCode.OAUTH_TIMEOUT,
            ) from exc

    def stop(self) -> None:
        """Stop serving and free the socket. Safe to call multiple times."""
        if self._stopped:
            return
        self._stopped = True
        server = self._server
        thread = self._thread
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:
                log.warning("Loopback shutdown raised: %s", exc)
            try:
                server.server_close()
            except Exception as exc:
                log.warning("Loopback close raised: %s", exc)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._server = None
        self._thread = None
