"""Small UI helpers ported from Hyprmod.

- :func:`confirm` — quick yes/no ``Adw.AlertDialog`` for destructive actions.
- :func:`try_with_toast` — run a callable; toast a friendly error and return
  ``False`` on failure, return ``True`` on success.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk


def confirm(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    label: str,
    on_confirm: Callable[[], object],
    *,
    cancel_label: str = "Cancel",
    appearance: Adw.ResponseAppearance = Adw.ResponseAppearance.DESTRUCTIVE,
) -> Adw.AlertDialog:
    """Present a simple confirmation dialog. Calls *on_confirm* if accepted.

    Use this for yes/no questions where the only inputs are the two response
    buttons. Form dialogs (with entry rows, live validation, or custom focus
    handling) should build ``Adw.AlertDialog`` directly — wrapping them here
    would obscure the form logic without saving meaningful boilerplate.
    """
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", cancel_label)
    dialog.add_response("confirm", label)
    dialog.set_response_appearance("confirm", appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "confirm":
            on_confirm()

    dialog.connect("response", on_response)
    dialog.present(parent)
    return dialog


def try_with_toast(
    show_toast: Callable[..., object],
    error_prefix: str,
    action: Callable[[], object],
    *,
    catch: type[BaseException] | tuple[type[BaseException], ...] = Exception,
    timeout: int = 5,
) -> bool:
    """Run *action*; toast and return ``False`` on caught error, else ``True``.

    Consolidates the common shape::

        try:
            do_thing()
            return True
        except SomeError as e:
            window.show_toast(f"... — {e}", timeout=5)
            return False

    *show_toast* is the bound method to call (typically ``window.show_toast``);
    the helper passes the formatted message as the first positional argument
    and ``timeout`` as a keyword argument, matching the
    ``show_toast(message, *, timeout=...)`` signature in this project.
    """
    try:
        action()
    except catch as e:
        show_toast(f"{error_prefix} — {e}", timeout=timeout)
        return False
    return True


__all__ = ["confirm", "try_with_toast"]
