"""Application state model.

Adapted from Hyprmod's ``core/state.py`` minus the Hyprland IPC layer. Holds
per-field ``live`` / ``saved`` / ``default`` triples so the upcoming Composite
API form can detect dirty fields, drive the dirty banner, and produce the
exact set of mutations to send in one transactional call.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ChangeCallback = Callable[[str], None]


@dataclass(slots=True)
class FieldState:
    """Live / saved / default values for a single tracked field."""

    live: Any
    saved: Any
    default: Any

    @property
    def is_dirty(self) -> bool:
        return self.live != self.saved


class AppState:
    """Tracks all form fields and notifies listeners on changes."""

    __slots__ = ("_fields", "_listeners")

    def __init__(self) -> None:
        self._fields: dict[str, FieldState] = {}
        self._listeners: list[ChangeCallback] = []

    def register(self, key: str, default: Any, saved: Any | None = None) -> None:
        """Register a new tracked field. ``saved`` falls back to ``default``."""
        if key in self._fields:
            raise ValueError(f"Field already registered: {key}")
        initial_saved = default if saved is None else saved
        self._fields[key] = FieldState(live=initial_saved, saved=initial_saved, default=default)

    def get(self, key: str) -> FieldState:
        return self._fields[key]

    def set_live(self, key: str, value: Any) -> None:
        """Update the live value of a field and notify listeners."""
        state = self._fields[key]
        if state.live == value:
            return
        state.live = value
        self._notify(key)

    def mark_saved(self, key: str | None = None) -> None:
        """Snapshot live → saved. With no key, mark every field saved."""
        keys = [key] if key is not None else list(self._fields.keys())
        for k in keys:
            state = self._fields[k]
            state.saved = state.live
            self._notify(k)

    def discard(self, key: str | None = None) -> None:
        """Reset live → saved (revert pending edits)."""
        keys = [key] if key is not None else list(self._fields.keys())
        for k in keys:
            state = self._fields[k]
            state.live = state.saved
            self._notify(k)

    @property
    def is_dirty(self) -> bool:
        return any(s.is_dirty for s in self._fields.values())

    def dirty_keys(self) -> list[str]:
        return [k for k, s in self._fields.items() if s.is_dirty]

    def on_change(self, callback: ChangeCallback) -> None:
        self._listeners.append(callback)

    def _notify(self, key: str) -> None:
        for cb in self._listeners:
            cb(key)
