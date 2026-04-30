"""Shared helpers for ``Adw.Dialog`` subclasses.

Houses :class:`SingletonDialogMixin` — a small mixin that collapses
fast double-clicks on a "show this dialog" button into a single
dialog instance. ``Adw.Dialog.present`` is asynchronous, so clicking
a trigger N times before GTK renders the first dialog would
otherwise queue N stacked dialogs.

Ported verbatim from Hyprmod's ``ui/dialog.py``.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk


class SingletonDialogMixin:
    """Mix into an ``Adw.Dialog`` subclass to make it open at most once.

    Use ``cls.present_singleton(parent, **kwargs)`` instead of
    ``cls(**kwargs).present(parent)``. Subsequent calls while a
    dialog of the same class is already open are silently no-ops; the
    slot is freed when the dialog emits ``closed``.

    Open dialogs are tracked in a class-keyed dict on the mixin
    itself, so each subclass gets an independent slot — an open
    ``AddOrgDialog`` won't suppress an ``OAuthProgressDialog``.
    """

    _open_instances: dict[type, Adw.Dialog] = {}

    @classmethod
    def present_singleton(cls, parent: Gtk.Widget, **kwargs: object) -> None:
        """Open the dialog under the at-most-one-open guard.

        ``parent`` is forwarded to ``Adw.Dialog.present``; remaining
        keyword arguments are forwarded to ``__init__``.
        """
        if cls in SingletonDialogMixin._open_instances:
            return
        # Pyright can't see that SingletonDialogMixin is mixed into an
        # Adw.Dialog subclass, so the type of ``cls(...)`` resolves to
        # ``SingletonDialogMixin``. The runtime constructor is the
        # dialog subclass itself.
        dialog = cls(**kwargs)  # type: ignore[call-arg]
        if not isinstance(dialog, Adw.Dialog):
            return
        SingletonDialogMixin._open_instances[cls] = dialog
        dialog.connect("closed", cls._on_singleton_closed)
        dialog.present(parent)

    @classmethod
    def _on_singleton_closed(cls, _dialog: Adw.Dialog) -> None:
        SingletonDialogMixin._open_instances.pop(cls, None)


__all__ = ["SingletonDialogMixin"]
