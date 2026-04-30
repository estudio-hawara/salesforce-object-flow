"""Main application window."""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Adw, Gdk, Gtk

from salesforce_object_flow.core.state import AppState
from salesforce_object_flow.pages.welcome import WelcomePage

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).resolve().parent / "style.css"


class MainWindow(Adw.ApplicationWindow):
    """Top-level window: sidebar + page stack inside a toast overlay."""

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.set_title("Salesforce Object Flow")
        self.set_default_size(1000, 700)

        self._state = AppState()
        self._state.on_change(self._on_state_change)

        self._load_css()
        self._build_ui()

    def _load_css(self) -> None:
        if not CSS_PATH.exists():
            log.warning("CSS file not found at %s", CSS_PATH)
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(CSS_PATH))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        split_view = Adw.NavigationSplitView()
        self._toast_overlay.set_child(split_view)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._sidebar_list = Gtk.ListBox()
        self._sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._sidebar_list.add_css_class("navigation-sidebar")
        self._sidebar_list.connect("row-selected", self._on_sidebar_selected)

        self._add_page(WelcomePage())

        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_child(self._sidebar_list)
        sidebar_scroll.set_vexpand(True)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="Salesforce Object Flow"))
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(sidebar_scroll)

        sidebar_page = Adw.NavigationPage(title="Salesforce Object Flow")
        sidebar_page.set_child(sidebar_toolbar)
        split_view.set_sidebar(sidebar_page)

        content_page = Adw.NavigationPage(title="Content")
        content_page.set_child(self._stack)
        split_view.set_content(content_page)

        first_row = self._sidebar_list.get_row_at_index(0)
        if first_row is not None:
            self._sidebar_list.select_row(first_row)

    def _add_page(self, page: WelcomePage) -> None:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=page.TITLE))
        toolbar_view = page.build(header)
        self._stack.add_titled(toolbar_view, page.TITLE, page.TITLE)

        row = Gtk.ListBoxRow()
        row.set_child(
            Gtk.Label(label=page.TITLE, xalign=0, margin_top=8, margin_bottom=8, margin_start=12)
        )
        # Store the stack page name on the row so selection can route to it.
        row.set_name(page.TITLE)
        self._sidebar_list.append(row)

    def _on_sidebar_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self._stack.set_visible_child_name(row.get_name())

    def _on_state_change(self, key: str) -> None:
        # Hook for the future Composite API form: refresh the dirty banner,
        # update window subtitle, etc.
        log.debug("AppState changed: key=%s is_dirty=%s", key, self._state.is_dirty)

    def show_toast(self, message: str, *, timeout: int = 3) -> None:
        """Display a transient toast at the bottom of the window."""
        toast = Adw.Toast(title=message, timeout=timeout)
        self._toast_overlay.add_toast(toast)
