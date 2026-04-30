"""Managed GLib timeout helper.

Ported from Hyprmod's ``ui/timer.py``.
"""

from collections.abc import Callable
from typing import Any

from gi.repository import GLib


class Timer:
    """A single GLib timeout that auto-cancels the previous one on reschedule.

    Usage:
        self._timer = Timer()
        self._timer.schedule(800, self._on_fire)   # (re)schedules
        self._timer.cancel()                       # safe even if not running
        if self._timer.active: ...                 # check state
    """

    __slots__ = ("_id",)

    def __init__(self) -> None:
        self._id: int | None = None

    @property
    def active(self) -> bool:
        return self._id is not None

    def schedule(self, delay_ms: int, callback: Callable[..., Any], *args: Any) -> None:
        """Cancel any pending timeout and schedule a new one."""
        self.cancel()
        self._id = GLib.timeout_add(delay_ms, self._fire, callback, args)

    def cancel(self) -> None:
        """Cancel the pending timeout if any."""
        if self._id is not None:
            GLib.source_remove(self._id)
            self._id = None

    def _fire(self, callback: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        try:
            result = callback(*args)
        except Exception:
            self._id = None
            raise
        if result != GLib.SOURCE_CONTINUE:
            self._id = None
        return result
