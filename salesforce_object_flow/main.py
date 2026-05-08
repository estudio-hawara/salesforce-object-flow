"""Application entry point."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from salesforce_object_flow import i18n
from salesforce_object_flow.core.logging_setup import setup_logging
from salesforce_object_flow.window import MainWindow

APP_ID = "es.hawara.SalesforceObjectFlow"


class SalesforceObjectFlowApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_startup(self) -> None:  # type: ignore[override]
        Adw.Application.do_startup(self)
        self._register_icons()

    def do_activate(self) -> None:  # type: ignore[override]
        win = self.props.active_window
        if win is None:
            win = MainWindow(application=self)
        win.present()

    def _register_icons(self) -> None:
        # Two candidate icon roots: one for the editable tree (data/icons), one
        # for the installed wheel (salesforce_object_flow/_data/icons via the
        # hatch ``force-include`` rule). Register both — missing dirs are a
        # no-op for Gtk.IconTheme.
        package_root = Path(__file__).resolve().parent
        candidates = [
            package_root / "_data" / "icons",
            package_root.parent / "data" / "icons",
        ]
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        existing = list(theme.get_search_path() or [])
        for path in candidates:
            if path.is_dir():
                existing.insert(0, str(path))
        theme.set_search_path(existing)


def main() -> int:
    i18n.init()
    setup_logging()
    app = SalesforceObjectFlowApp()

    # Route SIGINT/SIGTERM through the GLib main loop so Ctrl-C from the
    # terminal shuts the app down cleanly. ``GLib.unix_signal_add`` is
    # POSIX-only — guard it for Windows so the import path stays portable.
    if hasattr(GLib, "unix_signal_add"):

        def _on_signal(*_args: object) -> bool:
            app.quit()
            return GLib.SOURCE_REMOVE

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _on_signal)

    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
