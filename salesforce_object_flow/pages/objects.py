"""Object Explorer page.

Read-only browser for the active org's SObjects and their fields. Threads are
spawned for each network call; results land back on the GTK main loop via
``GLib.idle_add``. Stale results from a previous active org are detected with
monotonic int tokens and silently dropped.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from gi.repository import Adw, GLib, Gtk

from salesforce_object_flow.core.config import OrgEntry
from salesforce_object_flow.pages.groups import PageGroup
from salesforce_object_flow.services.sobjects import (
    SObjectDescribe,
    SObjectField,
    SObjectService,
    SObjectSummary,
)
from salesforce_object_flow.ui.timer import Timer

if TYPE_CHECKING:
    from salesforce_object_flow.window import MainWindow

log = logging.getLogger(__name__)

_SEARCH_DEBOUNCE_MS = 150


class ObjectExplorerPage:
    NAME: ClassVar[str] = "objects"
    TITLE: ClassVar[str] = "Object Explorer"
    ICON_NAME: ClassVar[str] = "loupe-large-symbolic"
    GROUP: ClassVar[PageGroup] = PageGroup.DATA_MODEL

    # ----- Lifecycle ------------------------------------------------------
    def __init__(
        self,
        window: MainWindow,
        sobjects: SObjectService,
        get_active_alias: Callable[[], str | None],
        get_active_entry: Callable[[], OrgEntry | None],
    ) -> None:
        self._window = window
        self._sobjects = sobjects
        self._get_active_alias = get_active_alias
        self._get_active_entry = get_active_entry

        self._list_request_token: int = 0
        self._describe_request_token: int = 0
        self._summaries: list[SObjectSummary] = []
        self._search_timer = Timer()
        self._current_alias: str | None = None
        self._current_describe_name: str | None = None

    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView:
        actual_header = header or Adw.HeaderBar()

        # Sandbox badge — visibility toggled in _update_sandbox_badge.
        self._sandbox_badge = Gtk.Label(label="Sandbox")
        self._sandbox_badge.add_css_class("option-managed")
        self._sandbox_badge.set_visible(False)
        actual_header.pack_start(self._sandbox_badge)

        # Refresh button on the right of the header.
        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.set_tooltip_text("Refresh SObject list from the active connection")
        self._refresh_btn.connect("clicked", self._on_refresh_clicked)
        actual_header.pack_end(self._refresh_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(actual_header)
        toolbar.set_content(self._build_split_view())

        # First render — even before the first active-org notification fires,
        # show whichever empty state matches the current state.
        self._current_alias = self._get_active_alias()
        self._update_sandbox_badge()
        self._render_list_state()
        if self._current_alias is not None:
            self._kick_off_list_fetch()

        return toolbar

    # ----- Window-driven entry points ------------------------------------
    def on_active_org_changed(self) -> None:
        """Called by ``MainWindow`` whenever the active alias may have changed."""
        # Increment tokens so any in-flight result for the old alias is dropped.
        self._list_request_token += 1
        self._describe_request_token += 1
        self._summaries = []
        self._current_alias = self._get_active_alias()
        self._current_describe_name = None
        if hasattr(self, "_search_entry"):
            self._search_entry.set_text("")
        self._update_sandbox_badge()
        self._render_list_state()
        self._render_detail_empty()
        if self._current_alias is not None:
            self._kick_off_list_fetch()

    # ----- Split view layout ---------------------------------------------
    def _build_split_view(self) -> Gtk.Widget:
        self._split = Adw.NavigationSplitView()
        self._split.set_min_sidebar_width(280)
        self._split.set_max_sidebar_width(420)
        self._split.set_sidebar_width_fraction(0.35)

        sidebar = Adw.NavigationPage(title="SObjects")
        sidebar.set_child(self._build_sidebar_pane())
        self._split.set_sidebar(sidebar)

        content = Adw.NavigationPage(title="Detail")
        content.set_child(self._build_content_pane())
        self._split.set_content(content)

        return self._split

    def _build_sidebar_pane(self) -> Gtk.Widget:
        # The sidebar pane uses a Gtk.Stack so the empty-state status page
        # can fully replace the search+list view, not crowd it.
        self._sidebar_stack = Gtk.Stack()
        self._sidebar_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # State: list view (search + listbox).
        list_box_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        list_box_outer.set_margin_top(12)
        list_box_outer.set_margin_bottom(12)
        list_box_outer.set_margin_start(12)
        list_box_outer.set_margin_end(12)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search SObjects…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        list_box_outer.append(self._search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_filter_func(self._filter_row)
        self._list_box.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self._list_box)
        list_box_outer.append(scrolled)

        self._sidebar_stack.add_named(list_box_outer, "list")

        # State: empty placeholders, including loading and errors.
        self._sidebar_stack.add_named(
            self._make_status_page(
                title="Loading SObjects…",
                description="Fetching metadata from the active connection.",
                icon_name="view-refresh-symbolic",
            ),
            "loading",
        )
        self._sidebar_stack.add_named(
            self._make_status_page(
                title="No active connection",
                description=(
                    "Activate a connection from the sidebar menu, or add one in Connections."
                ),
                icon_name="network-offline-symbolic",
            ),
            "no_active",
        )
        self._sidebar_stack.add_named(
            self._make_status_page(
                title="No connections yet",
                description="Add a Salesforce connection from the Connections page to begin.",
                icon_name="network-offline-symbolic",
                action_label="Go to Connections",
                action_name="win.go-to-connections",
            ),
            "no_connections",
        )
        self._list_error_status = self._make_status_page(
            title="Could not load objects",
            description="",
            icon_name="dialog-warning-symbolic",
        )
        self._list_error_retry_btn = Gtk.Button(label="Retry")
        self._list_error_retry_btn.add_css_class("pill")
        self._list_error_retry_btn.add_css_class("suggested-action")
        self._list_error_retry_btn.set_halign(Gtk.Align.CENTER)
        self._list_error_retry_btn.connect("clicked", self._on_list_retry)
        self._list_error_status.set_child(self._list_error_retry_btn)
        self._sidebar_stack.add_named(self._list_error_status, "error")
        self._sidebar_stack.add_named(
            self._make_status_page(
                title="No matches",
                description="Try a different name or label.",
                icon_name="system-search-symbolic",
            ),
            "no_matches",
        )

        return self._sidebar_stack

    def _build_content_pane(self) -> Gtk.Widget:
        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self._detail_stack.add_named(
            self._make_status_page(
                title="Select an SObject",
                description="Pick an SObject from the list to see its fields.",
                icon_name="document-properties-symbolic",
            ),
            "empty",
        )
        self._detail_stack.add_named(
            self._make_status_page(
                title="Loading fields…",
                description="",
                icon_name="view-refresh-symbolic",
            ),
            "loading",
        )

        self._detail_error_status = self._make_status_page(
            title="Could not load fields",
            description="",
            icon_name="dialog-warning-symbolic",
        )
        self._detail_error_retry_btn = Gtk.Button(label="Retry")
        self._detail_error_retry_btn.add_css_class("pill")
        self._detail_error_retry_btn.add_css_class("suggested-action")
        self._detail_error_retry_btn.set_halign(Gtk.Align.CENTER)
        self._detail_error_retry_btn.connect("clicked", self._on_detail_retry)
        self._detail_error_status.set_child(self._detail_error_retry_btn)
        self._detail_stack.add_named(self._detail_error_status, "error")

        # The actual detail view.
        self._detail_scroll = Gtk.ScrolledWindow()
        self._detail_scroll.set_vexpand(True)
        self._detail_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._detail_body.set_margin_top(18)
        self._detail_body.set_margin_bottom(18)
        self._detail_body.set_margin_start(18)
        self._detail_body.set_margin_end(18)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(800)
        clamp.set_child(self._detail_body)
        self._detail_scroll.set_child(clamp)
        self._detail_stack.add_named(self._detail_scroll, "detail")

        return self._detail_stack

    @staticmethod
    def _make_status_page(
        *,
        title: str,
        description: str,
        icon_name: str,
        action_label: str | None = None,
        action_name: str | None = None,
    ) -> Adw.StatusPage:
        page = Adw.StatusPage(title=title, description=description, icon_name=icon_name)
        if action_label and action_name:
            btn = Gtk.Button(label=action_label)
            btn.add_css_class("pill")
            btn.add_css_class("suggested-action")
            btn.set_halign(Gtk.Align.CENTER)
            btn.set_action_name(action_name)
            page.set_child(btn)
        return page

    # ----- Sandbox badge --------------------------------------------------
    def _update_sandbox_badge(self) -> None:
        if not hasattr(self, "_sandbox_badge"):
            return
        entry = self._get_active_entry()
        self._sandbox_badge.set_visible(entry is not None and entry.is_sandbox)

    # ----- List pane state machine ---------------------------------------
    def _render_list_state(self) -> None:
        if not hasattr(self, "_sidebar_stack"):
            return
        if self._current_alias is None:
            # Distinguish "no connections at all" from "some exist, none active".
            has_any = self._has_any_orgs()
            self._sidebar_stack.set_visible_child_name(
                "no_connections" if not has_any else "no_active"
            )
            self._refresh_btn.set_sensitive(False)
            return
        self._refresh_btn.set_sensitive(True)
        if not self._summaries:
            # Either still loading (token > 0) or empty list returned.
            self._sidebar_stack.set_visible_child_name("loading")
            return
        self._sidebar_stack.set_visible_child_name("list")
        self._update_list_empty_state()

    def _has_any_orgs(self) -> bool:
        # If the window passed us a get_active_entry that returns None, it
        # could mean either no orgs at all or no active alias. Distinguish via
        # the connections service surfaced through the page constructor.
        # We avoid taking a hard dep on ConnectionsService here; instead we
        # peek at MainWindow.config which is available via the toast helper.
        return bool(getattr(self._window, "config", None) and self._window.config.orgs)

    def _update_list_empty_state(self) -> None:
        # Called after a filter pass to flip between "list" and "no_matches".
        if self._current_alias is None or not self._summaries:
            return
        # Count visible rows.
        any_visible = False
        index = 0
        while True:
            row = self._list_box.get_row_at_index(index)
            if row is None:
                break
            if row.get_child_visible() and row.is_visible():
                any_visible = True
                break
            index += 1
        # GtkListBox visibility is driven by the filter; re-check via filter
        # function for a more robust answer.
        if not any_visible:
            term = self._search_entry.get_text().strip().lower()
            if term:
                # Show no-matches placeholder.
                self._sidebar_stack.set_visible_child_name("no_matches")
                return
        self._sidebar_stack.set_visible_child_name("list")

    # ----- List fetch -----------------------------------------------------
    def _kick_off_list_fetch(self) -> None:
        alias = self._current_alias
        if alias is None:
            return
        token = self._list_request_token = self._list_request_token + 1
        self._sidebar_stack.set_visible_child_name("loading")

        def worker() -> None:
            try:
                summaries = self._sobjects.list_sobjects(alias)
                GLib.idle_add(self._on_list_loaded, token, summaries)
            except Exception as exc:
                log.debug("list_sobjects failed", exc_info=True)
                GLib.idle_add(self._on_list_error, token, str(exc))

        threading.Thread(target=worker, daemon=True, name=f"sobj-list-{alias}").start()

    def _on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        alias = self._current_alias
        if alias is None:
            return
        token = self._list_request_token = self._list_request_token + 1
        self._sidebar_stack.set_visible_child_name("loading")

        def worker() -> None:
            try:
                summaries = self._sobjects.refresh_list(alias)
                GLib.idle_add(self._on_list_loaded, token, summaries)
            except Exception as exc:
                log.debug("refresh_list failed", exc_info=True)
                GLib.idle_add(self._on_list_error, token, str(exc))

        threading.Thread(target=worker, daemon=True, name=f"sobj-refresh-{alias}").start()

    def _on_list_retry(self, _btn: Gtk.Button) -> None:
        self._kick_off_list_fetch()

    def _on_list_loaded(self, token: int, summaries: list[SObjectSummary]) -> bool:
        if token != self._list_request_token:
            return False
        self._summaries = summaries
        self._populate_list_box()
        self._sidebar_stack.set_visible_child_name("list")
        self._update_list_empty_state()
        return False

    def _on_list_error(self, token: int, message: str) -> bool:
        if token != self._list_request_token:
            return False
        self._list_error_status.set_description(message)
        self._sidebar_stack.set_visible_child_name("error")
        self._window.show_toast(f"Could not load objects — {message}", timeout=6)
        return False

    def _populate_list_box(self) -> None:
        # Replace all children. set_filter_func keeps filtering working after.
        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)

        for summary in self._summaries:
            row = Adw.ActionRow()
            row.set_title(summary.label)
            row.set_subtitle(summary.name)
            row.set_activatable(True)
            if summary.custom:
                badge = Gtk.Label(label="Custom")
                badge.add_css_class("option-managed")
                badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(badge)
            # Stash summary on the wrapper so the filter can read it.
            row._summary = summary  # type: ignore[attr-defined]  # noqa: SLF001
            self._list_box.append(row)

        self._list_box.invalidate_filter()

    # ----- Search ---------------------------------------------------------
    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._search_timer.schedule(_SEARCH_DEBOUNCE_MS, self._apply_filter)

    def _apply_filter(self) -> bool:
        self._list_box.invalidate_filter()
        self._update_list_empty_state()
        return GLib.SOURCE_REMOVE

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        if not hasattr(self, "_search_entry"):
            return True
        term = self._search_entry.get_text().strip().lower()
        if not term:
            return True
        summary: SObjectSummary | None = getattr(row, "_summary", None)
        if summary is None:
            return False
        return term in summary.name.lower() or term in summary.label.lower()

    # ----- Detail pane ----------------------------------------------------
    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        summary: SObjectSummary | None = getattr(row, "_summary", None)
        if summary is None or self._current_alias is None:
            return
        self._current_describe_name = summary.name
        self._render_detail_loading(summary)

        token = self._describe_request_token = self._describe_request_token + 1
        alias = self._current_alias
        name = summary.name

        def worker() -> None:
            try:
                describe = self._sobjects.describe(alias, name)
                GLib.idle_add(self._on_describe_loaded, token, describe, summary)
            except Exception as exc:
                log.debug("describe failed", exc_info=True)
                GLib.idle_add(self._on_describe_error, token, name, str(exc))

        threading.Thread(target=worker, daemon=True, name=f"sobj-describe-{name}").start()

    def _on_detail_retry(self, _btn: Gtk.Button) -> None:
        if self._current_describe_name is None or self._current_alias is None:
            return
        # Reuse the activation path by faking a re-click on the matching row.
        index = 0
        while True:
            row = self._list_box.get_row_at_index(index)
            if row is None:
                return
            summary: SObjectSummary | None = getattr(row, "_summary", None)
            if summary is not None and summary.name == self._current_describe_name:
                self._on_row_activated(self._list_box, row)
                return
            index += 1

    def _render_detail_empty(self) -> None:
        if hasattr(self, "_detail_stack"):
            self._detail_stack.set_visible_child_name("empty")

    def _render_detail_loading(self, summary: SObjectSummary) -> None:
        if hasattr(self, "_detail_stack"):
            self._detail_stack.set_visible_child_name("loading")
            log.debug("Loading describe for %s", summary.name)

    def _on_describe_loaded(
        self,
        token: int,
        describe: SObjectDescribe,
        summary: SObjectSummary,
    ) -> bool:
        if token != self._describe_request_token:
            return False
        self._populate_detail(describe, summary)
        self._detail_stack.set_visible_child_name("detail")
        return False

    def _on_describe_error(self, token: int, name: str, message: str) -> bool:
        if token != self._describe_request_token:
            return False
        self._detail_error_status.set_title(f"Could not load fields for “{name}”")
        self._detail_error_status.set_description(message)
        self._detail_stack.set_visible_child_name("error")
        self._window.show_toast(f"“{name}”: {message}", timeout=6)
        return False

    def _populate_detail(self, describe: SObjectDescribe, summary: SObjectSummary) -> None:
        # Drop previous content.
        while True:
            child = self._detail_body.get_first_child()
            if child is None:
                break
            self._detail_body.remove(child)

        summary_group = Adw.PreferencesGroup()
        summary_group.set_title(describe.label)
        summary_group.set_description(describe.name)
        summary_group.add(self._build_summary_row(summary))
        self._detail_body.append(summary_group)

        fields_group = Adw.PreferencesGroup()
        fields_group.set_title(f"Fields ({len(describe.fields)})")
        for field in describe.fields:
            fields_group.add(self._build_field_row(field))
        self._detail_body.append(fields_group)

    def _build_summary_row(self, summary: SObjectSummary) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title("Capabilities")
        flags: list[str] = []
        if summary.queryable:
            flags.append("queryable")
        if summary.createable:
            flags.append("createable")
        if summary.updateable:
            flags.append("updateable")
        if summary.deletable:
            flags.append("deletable")
        if summary.custom:
            flags.append("custom")
        row.set_subtitle(" · ".join(flags) if flags else "none")
        return row

    def _build_field_row(self, field: SObjectField) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(field.label)
        row.set_subtitle(f"{field.name} · {self._format_field_type(field)}")
        if not field.nillable and field.createable:
            badge = Gtk.Label(label="required")
            badge.add_css_class("option-dirty")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)
        return row

    @staticmethod
    def _format_field_type(field: SObjectField) -> str:
        if field.type == "reference" and field.reference_to:
            return f"reference → {', '.join(field.reference_to)}"
        if field.type == "picklist":
            count = len(field.picklist_values)
            return f"picklist · {count} values"
        if field.length and field.length > 0 and field.type in {"string", "textarea"}:
            return f"{field.type}({field.length})"
        return field.type
