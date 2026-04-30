"""Main application window."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from salesforce_object_flow.core.cache import default_cache
from salesforce_object_flow.core.config import Config, OrgEntry
from salesforce_object_flow.core.state import AppState
from salesforce_object_flow.pages.connections import ConnectionsPage
from salesforce_object_flow.pages.formats import FileFormatsPage
from salesforce_object_flow.pages.objects import ObjectExplorerPage
from salesforce_object_flow.pages.welcome import WelcomePage
from salesforce_object_flow.services.connections import ConnectionsService
from salesforce_object_flow.services.formats import FileFormatStore, FileFormatValidator
from salesforce_object_flow.services.sobjects import SObjectService

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).resolve().parent / "style.css"


class _Page(Protocol):
    TITLE: ClassVar[str]

    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView: ...


class MainWindow(Adw.ApplicationWindow):
    """Top-level window: sidebar + page stack inside a toast overlay."""

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.set_title("Salesforce Object Flow")
        self.set_default_size(1000, 700)

        self._state = AppState()
        self._state.on_change(self._on_state_change)

        self._config = Config.load()
        self._service = ConnectionsService(
            config=self._config,
            config_save=self._config.save,
        )
        self._sobjects_service = SObjectService(self._service, default_cache())
        self._formats_store = FileFormatStore()
        self._formats_validator = FileFormatValidator()
        self._connections_page: ConnectionsPage | None = None
        self._formats_page: FileFormatsPage | None = None
        self._objects_page: ObjectExplorerPage | None = None
        self._active_org_button: Gtk.MenuButton | None = None
        self._active_org_subscribers: list[Callable[[], None]] = []

        self._load_css()
        self._build_ui()
        self._install_active_org_actions()

        landing_title = "Connections" if self._config.orgs else "Welcome"
        self._select_sidebar_row_by_title(landing_title)

    @property
    def config(self) -> Config:
        return self._config

    @property
    def service(self) -> ConnectionsService:
        return self._service

    # ------------------------------------------------------------ CSS / UI
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
        self._connections_page = ConnectionsPage(
            window=self,
            service=self._service,
            on_orgs_changed=self._refresh_active_org_menu,
            on_active_org_changed=self._notify_active_org_changed,
        )
        self._add_page(self._connections_page)
        self._formats_page = FileFormatsPage(
            window=self,
            store=self._formats_store,
            validator=self._formats_validator,
        )
        self._add_page(self._formats_page)
        self._objects_page = ObjectExplorerPage(
            window=self,
            sobjects=self._sobjects_service,
            get_active_alias=self._get_active_alias,
            get_active_entry=self._get_active_entry,
        )
        self._add_page(self._objects_page)
        self._active_org_subscribers.append(self._objects_page.on_active_org_changed)

        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_child(self._sidebar_list)
        sidebar_scroll.set_vexpand(True)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="Salesforce Object Flow"))
        self._active_org_button = Gtk.MenuButton()
        self._active_org_button.set_icon_name("network-server-symbolic")
        self._active_org_button.set_tooltip_text("Active Salesforce org")
        self._active_org_button.add_css_class("flat")
        sidebar_header.pack_end(self._active_org_button)
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(sidebar_scroll)

        sidebar_page = Adw.NavigationPage(title="Salesforce Object Flow")
        sidebar_page.set_child(sidebar_toolbar)
        split_view.set_sidebar(sidebar_page)

        content_page = Adw.NavigationPage(title="Content")
        content_page.set_child(self._stack)
        split_view.set_content(content_page)

    def _add_page(self, page: _Page) -> None:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=page.TITLE))
        toolbar_view = page.build(header)
        self._stack.add_titled(toolbar_view, page.TITLE, page.TITLE)

        row = Gtk.ListBoxRow()
        row.set_child(
            Gtk.Label(label=page.TITLE, xalign=0, margin_top=8, margin_bottom=8, margin_start=12)
        )
        row.set_name(page.TITLE)
        self._sidebar_list.append(row)

    def _on_sidebar_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self._stack.set_visible_child_name(row.get_name())

    def _select_sidebar_row_by_title(self, title: str) -> None:
        index = 0
        while True:
            row = self._sidebar_list.get_row_at_index(index)
            if row is None:
                return
            if row.get_name() == title:
                self._sidebar_list.select_row(row)
                return
            index += 1

    def _on_state_change(self, key: str) -> None:
        log.debug("AppState changed: key=%s is_dirty=%s", key, self._state.is_dirty)

    # --------------------------------------------------------------- Toast
    def show_toast(self, message: str, *, timeout: int = 3) -> None:
        """Display a transient toast at the bottom of the window."""
        toast = Adw.Toast(title=message, timeout=timeout)
        self._toast_overlay.add_toast(toast)

    # --------------------------------------------------------- Active org
    def _install_active_org_actions(self) -> None:
        activate_action = Gio.SimpleAction.new("activate-org", GLib.VariantType.new("s"))
        activate_action.connect("activate", self._on_action_activate_org)
        self.add_action(activate_action)

        remove_action = Gio.SimpleAction.new("remove-org", GLib.VariantType.new("s"))
        remove_action.connect("activate", self._on_action_remove_org)
        self.add_action(remove_action)

        go_action = Gio.SimpleAction.new("go-to-connections", None)
        go_action.connect("activate", self._on_action_go_to_connections)
        self.add_action(go_action)

        self._refresh_active_org_menu()

    def _on_action_activate_org(
        self, _action: Gio.SimpleAction, parameter: GLib.Variant | None
    ) -> None:
        if parameter is None:
            return
        alias = parameter.get_string()
        try:
            self._service.set_active(alias)
        except Exception as exc:
            self.show_toast(str(exc), timeout=5)
            return
        self.show_toast(f"Active org: “{alias}”.")
        self._refresh_active_org_menu()
        if self._connections_page is not None:
            self._connections_page.refresh_org_list()
        self._notify_active_org_changed()

    def _on_action_remove_org(
        self, _action: Gio.SimpleAction, parameter: GLib.Variant | None
    ) -> None:
        if parameter is None or self._connections_page is None:
            return
        self._connections_page.request_remove(parameter.get_string())

    def _on_action_go_to_connections(
        self, _action: Gio.SimpleAction, _parameter: GLib.Variant | None
    ) -> None:
        self._select_sidebar_row_by_title("Connections")

    def _refresh_active_org_menu(self) -> None:
        if self._active_org_button is None:
            return

        menu = Gio.Menu()
        if self._config.orgs:
            orgs_section = Gio.Menu()
            for entry in self._config.orgs:
                item = Gio.MenuItem.new(entry.alias, None)
                item.set_action_and_target_value(
                    "win.activate-org", GLib.Variant.new_string(entry.alias)
                )
                orgs_section.append_item(item)
            menu.append_section(None, orgs_section)
        actions_section = Gio.Menu()
        actions_section.append("Add org…", "win.go-to-connections")
        menu.append_section(None, actions_section)
        self._active_org_button.set_menu_model(menu)

        active = self._config.active_org_alias
        self._active_org_button.set_label(active or "No active org")
        self._active_org_button.set_always_show_arrow(True)

    def _get_active_alias(self) -> str | None:
        return self._config.active_org_alias

    def _get_active_entry(self) -> OrgEntry | None:
        alias = self._config.active_org_alias
        if alias is None:
            return None
        return self._config.find_org(alias)

    def _notify_active_org_changed(self) -> None:
        for cb in self._active_org_subscribers:
            try:
                cb()
            except Exception:
                log.exception("Active-org subscriber raised")
