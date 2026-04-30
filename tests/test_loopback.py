"""Tests for ``services/loopback.py``.

Always bind ephemeral ports (``port=0``) so tests never collide with the
production callback port (8765). Use stdlib ``urllib`` rather than ``httpx``
so the loopback layer has no httpx coupling.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request

import pytest

from salesforce_object_flow.services.loopback import (
    CallbackResult,
    LoopbackError,
    LoopbackServer,
)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_callback_resolves_future() -> None:
    server = LoopbackServer("expected-state", port=0)
    server.start()
    try:
        status, body = _get(
            f"http://127.0.0.1:{server.port}/callback?code=ABC&state=expected-state"
        )
        assert status == 200
        assert "close this tab" in body.lower()
        assert server.wait(timeout=2.0) == CallbackResult(code="ABC", state="expected-state")
    finally:
        server.stop()


def test_state_mismatch_raises_loopback_error() -> None:
    server = LoopbackServer("expected-state", port=0)
    server.start()
    try:
        status, body = _get(f"http://127.0.0.1:{server.port}/callback?code=X&state=other")
        assert status == 400
        assert "state mismatch" in body.lower()
        with pytest.raises(LoopbackError) as exc:
            server.wait(timeout=2.0)
        assert "state mismatch" in str(exc.value).lower()
    finally:
        server.stop()


def test_error_param_raises_loopback_error() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    try:
        status, body = _get(
            f"http://127.0.0.1:{server.port}/callback?error=access_denied"
            f"&error_description=user%20cancelled"
        )
        assert status == 400
        assert "user cancelled" in body.lower()
        with pytest.raises(LoopbackError) as exc:
            server.wait(timeout=2.0)
        assert "access_denied" in str(exc.value)
        assert "user cancelled" in str(exc.value)
    finally:
        server.stop()


def test_unrelated_path_is_ignored_then_callback_resolves() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    try:
        # Browser pre-flighting favicon should not resolve the future.
        status, _body = _get(f"http://127.0.0.1:{server.port}/favicon.ico")
        assert status == 404

        # Real callback still works.
        status, _body = _get(f"http://127.0.0.1:{server.port}/callback?code=Z&state=st")
        assert status == 200
        result = server.wait(timeout=2.0)
        assert result == CallbackResult(code="Z", state="st")
    finally:
        server.stop()


def test_missing_code_param_raises() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    try:
        status, _body = _get(f"http://127.0.0.1:{server.port}/callback?state=st")
        assert status == 400
        with pytest.raises(LoopbackError):
            server.wait(timeout=2.0)
    finally:
        server.stop()


def test_port_in_use_raises_loopback_error() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    try:
        server = LoopbackServer("st", port=port)
        with pytest.raises(LoopbackError) as exc:
            server.start()
        assert str(port) in str(exc.value)
    finally:
        blocker.close()


def test_stop_is_idempotent() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    server.stop()
    server.stop()  # must not raise


def test_stop_before_start_is_safe() -> None:
    server = LoopbackServer("st", port=0)
    server.stop()  # must not raise


def test_wait_timeout_raises_loopback_error() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    try:
        with pytest.raises(LoopbackError) as exc:
            server.wait(timeout=0.2)
        assert "timed out" in str(exc.value).lower()
    finally:
        server.stop()


def test_concurrent_callbacks_only_first_wins() -> None:
    server = LoopbackServer("st", port=0)
    server.start()
    try:
        results: list[tuple[int, str]] = []
        threads = [
            threading.Thread(
                target=lambda i=i: results.append(
                    _get(f"http://127.0.0.1:{server.port}/callback?code=C{i}&state=st")
                )
            )
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        result = server.wait(timeout=2.0)
        # The future is set exactly once; whichever request raced first wins.
        assert result.state == "st"
        assert result.code in {"C0", "C1", "C2"}
    finally:
        server.stop()
