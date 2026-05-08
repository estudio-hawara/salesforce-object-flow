"""Composite Templates page — CRUD on Composite REST API templates.

Sister page to :mod:`pages.formats`: same NavigationSplitView skeleton, same
dirty-tracking + three-way confirm flow. Extra wrinkles:

- Each template links to exactly one :class:`FileFormat` by its on-disk
  filename. If the linked format is missing, the page shows an urgent
  banner and disables Save / Preview until the user picks another format.
- The body of each subrequest is a list of typed key/value pairs (field
  name + value). The value can be a literal string, a single
  ``{{col}}`` placeholder (typed substitution from the linked format),
  or a string with embedded placeholders. The body is optional — leave
  it empty for GET / DELETE or for fire-and-forget POSTs.
- A "Preview payload" button switches the detail stack to a third state
  showing the rendered Composite payload (substituting ``{{col}}``
  placeholders against either a synthetic sample row or the first row of
  a user-picked CSV).
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from gi.repository import Adw, Gio, GLib, Gtk

from salesforce_object_flow.core.composite import (
    MAX_SUBREQUESTS,
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)
from salesforce_object_flow.core.formats import FileFormat, slugify
from salesforce_object_flow.pages.groups import PageGroup
from salesforce_object_flow.services.composite import (
    CompositeExecutor,
    CompositePayloadRenderer,
    CompositeTemplateError,
    CompositeTemplateStore,
    CompositeTemplateValidator,
    ExecutionError,
    ExecutionReport,
    LoadedTemplate,
    ProgressEvent,
    RenderRow,
    RowResult,
    export_failures_csv,
)
from salesforce_object_flow.services.connections import ConnectionsError, ConnectionsService
from salesforce_object_flow.services.formats import FileFormatStore
from salesforce_object_flow.ui.helpers import confirm

if TYPE_CHECKING:
    from salesforce_object_flow.window import MainWindow

log = logging.getLogger(__name__)

_HTTP_METHODS: tuple[HttpMethod, ...] = tuple(HttpMethod)


class CompositeTemplatesPage:
    NAME: ClassVar[str] = "composite"
    TITLE: ClassVar[str] = "Composite Requests"
    ICON_NAME: ClassVar[str] = "build-symbolic"
    GROUP: ClassVar[PageGroup] = PageGroup.RUN

    def __init__(
        self,
        window: MainWindow,
        store: CompositeTemplateStore,
        validator: CompositeTemplateValidator,
        renderer: CompositePayloadRenderer,
        formats_store: FileFormatStore,
        service: ConnectionsService,
        get_active_alias: Callable[[], str | None],
    ) -> None:
        self._window = window
        self._store = store
        self._validator = validator
        self._renderer = renderer
        self._formats_store = formats_store
        self._service = service
        self._get_active_alias = get_active_alias
        self._executor = CompositeExecutor(renderer=renderer)

        self._loaded: list[LoadedTemplate] = []
        self._selected_filename: str | None = None
        self._editing: CompositeTemplate | None = None
        self._original: CompositeTemplate | None = None
        self._suppress_dirty: bool = False
        self._format_filenames_in_combo: list[str] = []
        self._missing_format_inserted: bool = False
        self._subrequest_expanders: dict[int, Adw.ExpanderRow] = {}
        self._body_sections: dict[int, Gtk.ListBoxRow] = {}
        self._headers_sections: dict[int, Gtk.ListBoxRow] = {}

        # Run state.
        self._cancelled: threading.Event | None = None
        self._last_report: ExecutionReport | None = None
        self._last_report_csv_path: Path | None = None
        self._last_report_fmt: FileFormat | None = None
        self._last_report_template_name: str = ""

    # ---------------------------------------------------------------- Build
    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView:
        actual_header = header or Adw.HeaderBar()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(actual_header)
        toolbar.set_content(self._build_split_view())

        self._refresh_list()
        return toolbar

    def _build_split_view(self) -> Gtk.Widget:
        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(280)
        split.set_max_sidebar_width(420)
        split.set_sidebar_width_fraction(0.35)
        split.set_sidebar(self._build_sidebar_page())
        split.set_content(self._build_content_page())
        return split

    # --------------------------------------------------------- Sidebar (left)
    def _build_sidebar_page(self) -> Adw.NavigationPage:
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="Templates"))
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)

        new_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_btn.add_css_class("flat")
        new_btn.set_tooltip_text("Create a new template")
        new_btn.connect("clicked", self._on_new_clicked)
        sidebar_header.pack_end(new_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Reload templates from disk")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        sidebar_header.pack_end(refresh_btn)

        sidebar_toolbar.add_top_bar(sidebar_header)

        self._sidebar_stack = Gtk.Stack()
        self._sidebar_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        list_holder = Gtk.ScrolledWindow()
        list_holder.set_vexpand(True)
        list_holder.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("navigation-sidebar")
        self._list_box.connect("row-selected", self._on_row_selected)
        list_holder.set_child(self._list_box)
        self._sidebar_stack.add_named(list_holder, "list")

        empty = Adw.StatusPage(
            title="No templates yet",
            description="Create your first Composite template to start composing.",
            icon_name="document-properties-symbolic",
        )
        empty_btn = Gtk.Button(label="New template")
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", self._on_new_clicked)
        empty.set_child(empty_btn)
        self._sidebar_stack.add_named(empty, "empty")

        sidebar_toolbar.set_content(self._sidebar_stack)
        page = Adw.NavigationPage(title="Templates")
        page.set_child(sidebar_toolbar)
        return page

    # ---------------------------------------------------------- Detail (right)
    def _build_content_page(self) -> Adw.NavigationPage:
        content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()
        content_header.set_show_title(False)
        content_header.set_show_start_title_buttons(False)
        content_header.set_show_end_title_buttons(False)
        self._content_header = content_header

        self._preview_btn = Gtk.Button(
            label="Preview payload",
            icon_name="view-reveal-symbolic",
        )
        self._preview_btn.add_css_class("flat")
        self._preview_btn.set_sensitive(False)
        self._preview_btn.connect("clicked", self._on_preview_clicked)
        content_header.pack_start(self._preview_btn)

        self._run_btn = Gtk.Button(
            label="Run…",
            icon_name="media-playback-start-symbolic",
        )
        self._run_btn.add_css_class("flat")
        self._run_btn.set_sensitive(False)
        self._run_btn.set_tooltip_text("Execute against active connection")
        self._run_btn.connect("clicked", self._on_run_clicked)
        content_header.pack_start(self._run_btn)

        # "Back to editor" lives at the same pack_start slot as Preview
        # so the results pane's exit button visually replaces it.
        self._results_back_btn = Gtk.Button(
            label="Back to editor", icon_name="go-previous-symbolic"
        )
        self._results_back_btn.add_css_class("flat")
        self._results_back_btn.set_visible(False)
        self._results_back_btn.connect("clicked", self._on_back_to_editor)
        content_header.pack_start(self._results_back_btn)

        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save_clicked)
        content_header.pack_end(self._save_btn)

        self._delete_btn = Gtk.Button(label="Delete")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)
        content_header.pack_end(self._delete_btn)

        # "Export failures CSV…" lives at the same pack_end slot as Save.
        self._export_btn = Gtk.Button(
            label="Export failures CSV…", icon_name="document-save-as-symbolic"
        )
        self._export_btn.add_css_class("flat")
        self._export_btn.set_visible(False)
        self._export_btn.connect("clicked", self._on_export_failures_clicked)
        content_header.pack_end(self._export_btn)

        content_toolbar.add_top_bar(content_header)

        self._missing_banner = Adw.Banner(title="Linked format not found")
        self._missing_banner.add_css_class("confirm-urgent")
        self._missing_banner.set_button_label("Pick another format")
        self._missing_banner.connect("button-clicked", self._on_pick_another_format)
        content_toolbar.add_top_bar(self._missing_banner)

        self._unsaved_banner = Adw.Banner(title="Unsaved changes")
        self._unsaved_banner.add_css_class("confirm-warning")
        content_toolbar.add_top_bar(self._unsaved_banner)

        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        empty_status = Adw.StatusPage(
            title="Select or create a template",
            description=("Pick a template from the left, or click + to create a new one."),
            icon_name="document-properties-symbolic",
        )
        self._detail_stack.add_named(empty_status, "empty")

        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_vexpand(True)
        editor_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        editor_clamp = Adw.Clamp()
        editor_clamp.set_maximum_size(900)
        editor_clamp.set_child(self._build_editor_body())
        editor_scroll.set_child(editor_clamp)
        self._detail_stack.add_named(editor_scroll, "editor")

        self._detail_stack.add_named(self._build_preview_pane(), "preview")
        self._detail_stack.add_named(self._build_running_pane(), "running")
        self._detail_stack.add_named(self._build_results_pane(), "results")

        content_toolbar.set_content(self._detail_stack)
        page = Adw.NavigationPage(title="Detail")
        page.set_child(content_toolbar)
        return page

    # ----------------------------------------------------------- Editor body
    def _build_editor_body(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)

        general = Adw.PreferencesGroup()
        general.set_title("General")
        self._name_row = Adw.EntryRow()
        self._name_row.set_title("Name")
        self._name_row.connect("notify::text", self._on_name_changed)
        general.add(self._name_row)

        self._description_row = Adw.EntryRow()
        self._description_row.set_title("Description")
        self._description_row.connect("notify::text", self._on_description_changed)
        general.add(self._description_row)
        body.append(general)

        linkage = Adw.PreferencesGroup()
        linkage.set_title("Linkage")
        self._format_row = Adw.ComboRow()
        self._format_row.set_title("File format")
        self._format_row.set_subtitle("Columns of this format drive {{placeholder}} substitution.")
        self._format_model = Gtk.StringList()
        self._format_row.set_model(self._format_model)
        self._format_row.connect("notify::selected", self._on_format_changed)
        linkage.add(self._format_row)
        body.append(linkage)

        behavior = Adw.PreferencesGroup()
        behavior.set_title("Behavior")
        self._all_or_none_row = Adw.SwitchRow()
        self._all_or_none_row.set_title("All or none")
        self._all_or_none_row.set_subtitle("Roll back the whole batch if any subrequest fails.")
        self._all_or_none_row.connect("notify::active", self._on_all_or_none_changed)
        behavior.add(self._all_or_none_row)

        self._collate_row = Adw.SwitchRow()
        self._collate_row.set_title("Collate subrequests")
        self._collate_row.set_subtitle(
            "Group consecutive same-method subrequests in one round trip."
        )
        self._collate_row.connect("notify::active", self._on_collate_changed)
        behavior.add(self._collate_row)
        body.append(behavior)

        self._subrequests_group = Adw.PreferencesGroup()
        self._subrequests_group.set_title("Subrequests")
        self._add_sub_btn = Gtk.Button(label="Add subrequest", icon_name="list-add-symbolic")
        self._add_sub_btn.add_css_class("flat")
        self._add_sub_btn.connect("clicked", self._on_add_subrequest)
        self._subrequests_group.set_header_suffix(self._add_sub_btn)
        body.append(self._subrequests_group)

        self._editor_body = body
        return body

    def _build_preview_pane(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        back_btn = Gtk.Button(label="Back to editor", icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", self._on_back_to_editor)
        top.append(back_btn)

        sample_label = Gtk.Label(label="Sample row:")
        sample_label.add_css_class("dim-label")
        sample_label.set_valign(Gtk.Align.CENTER)
        top.append(sample_label)

        self._sample_dropdown = Gtk.DropDown.new_from_strings(["Synthetic", "From CSV…"])
        self._sample_dropdown.connect("notify::selected", self._on_sample_changed)
        top.append(self._sample_dropdown)
        outer.append(top)

        self._preview_summary = Gtk.Label(xalign=0, wrap=True)
        self._preview_summary.add_css_class("title-3")
        outer.append(self._preview_summary)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._preview_textview = Gtk.TextView()
        self._preview_textview.set_editable(False)
        self._preview_textview.set_monospace(True)
        self._preview_textview.set_top_margin(8)
        self._preview_textview.set_bottom_margin(8)
        self._preview_textview.set_left_margin(8)
        self._preview_textview.set_right_margin(8)
        scroll.set_child(self._preview_textview)
        outer.append(scroll)

        return outer

    def _build_running_pane(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(24)
        outer.set_margin_bottom(24)
        outer.set_margin_start(24)
        outer.set_margin_end(24)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.CENTER)

        spinner = Gtk.Spinner()
        spinner.set_size_request(48, 48)
        spinner.start()
        outer.append(spinner)
        self._run_spinner = spinner

        title = Gtk.Label(xalign=0)
        title.add_css_class("title-2")
        outer.append(title)
        self._run_title_label = title

        self._run_progress_label = Gtk.Label(xalign=0)
        self._run_progress_label.add_css_class("dim-label")
        outer.append(self._run_progress_label)

        self._run_progress_bar = Gtk.ProgressBar()
        self._run_progress_bar.set_size_request(360, -1)
        outer.append(self._run_progress_bar)

        self._run_last_error = Gtk.Label(xalign=0, wrap=True)
        self._run_last_error.add_css_class("error")
        self._run_last_error.set_visible(False)
        outer.append(self._run_last_error)

        self._run_cancel_btn = Gtk.Button(label="Cancel")
        self._run_cancel_btn.add_css_class("destructive-action")
        self._run_cancel_btn.set_halign(Gtk.Align.CENTER)
        self._run_cancel_btn.connect("clicked", self._on_cancel_clicked)
        outer.append(self._run_cancel_btn)

        return outer

    def _build_results_pane(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        self._results_banner = Adw.Banner(title="")
        self._results_banner.set_revealed(True)
        outer.append(self._results_banner)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        self._results_listbox = Gtk.ListBox()
        self._results_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._results_listbox.add_css_class("boxed-list")
        clamp.set_child(self._results_listbox)
        scroll.set_child(clamp)
        outer.append(scroll)

        return outer

    # ------------------------------------------------------------- Sidebar list
    def _refresh_list(self) -> None:
        try:
            self._loaded = self._store.list_templates()
        except Exception as exc:
            log.exception("Could not list templates")
            self._window.show_toast(f"Could not list templates — {exc}", timeout=6)
            self._loaded = []

        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)

        for loaded in self._loaded:
            row = Adw.ActionRow()
            row.set_title(loaded.template.name)
            if loaded.template.description:
                row.set_subtitle(loaded.template.description)
            row.set_activatable(True)
            row._loaded = loaded  # type: ignore[attr-defined]  # noqa: SLF001
            self._list_box.append(row)

        self._sidebar_stack.set_visible_child_name("list" if self._loaded else "empty")

        if self._selected_filename is not None:
            self._select_row_by_filename(self._selected_filename)
        if self._selected_filename is None:
            self._show_empty_detail()

    def _on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        self._maybe_discard_then(self._refresh_list)

    def _select_row_by_filename(self, filename: str) -> None:
        index = 0
        while True:
            row = self._list_box.get_row_at_index(index)
            if row is None:
                self._selected_filename = None
                return
            loaded: LoadedTemplate | None = getattr(row, "_loaded", None)
            if loaded is not None and loaded.filename == filename:
                self._suppress_dirty = True
                try:
                    self._list_box.select_row(row)
                finally:
                    self._suppress_dirty = False
                return
            index += 1

    def _row_for_filename(self, filename: str | None) -> Gtk.ListBoxRow | None:
        if filename is None:
            return None
        index = 0
        while True:
            row = self._list_box.get_row_at_index(index)
            if row is None:
                return None
            loaded: LoadedTemplate | None = getattr(row, "_loaded", None)
            if loaded is not None and loaded.filename == filename:
                return row
            index += 1

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._suppress_dirty:
            return
        if row is None:
            return
        loaded: LoadedTemplate | None = getattr(row, "_loaded", None)
        if loaded is None:
            return
        if loaded.filename == self._selected_filename:
            return

        if self._is_run_in_progress():
            previous = self._selected_filename
            self._suppress_dirty = True
            try:
                self._list_box.select_row(self._row_for_filename(previous))
            finally:
                self._suppress_dirty = False
            self._window.show_toast(
                "A run is in progress — cancel it before switching templates.",
                timeout=6,
            )
            return

        if self._is_dirty():
            previous = self._selected_filename
            self._suppress_dirty = True
            try:
                self._list_box.select_row(self._row_for_filename(previous))
            finally:
                self._suppress_dirty = False
            self._prompt_unsaved_changes(
                proceed=lambda: self._switch_to_template(loaded.filename),
            )
            return
        self._switch_to_template(loaded.filename)

    def _switch_to_template(self, filename: str) -> None:
        loaded = next((lt for lt in self._loaded if lt.filename == filename), None)
        if loaded is None:
            return
        self._selected_filename = filename
        self._editing = copy.deepcopy(loaded.template)
        self._original = copy.deepcopy(loaded.template)
        self._populate_editor()
        self._select_row_by_filename(filename)
        self._detail_stack.set_visible_child_name("editor")
        self._update_editor_chrome_visibility()
        self._delete_btn.set_sensitive(True)
        self._update_dirty_state()

    def _show_empty_detail(self) -> None:
        self._editing = None
        self._original = None
        self._delete_btn.set_sensitive(False)
        self._save_btn.set_sensitive(False)
        self._preview_btn.set_sensitive(False)
        self._run_btn.set_sensitive(False)
        self._unsaved_banner.set_revealed(False)
        self._missing_banner.set_revealed(False)
        self._detail_stack.set_visible_child_name("empty")
        self._update_editor_chrome_visibility()

    # ------------------------------------------------------------ New template
    def _on_new_clicked(self, _btn: Gtk.Button) -> None:
        def proceed() -> None:
            existing = {lt.filename for lt in self._loaded}
            new_name = self._unique_display_name(
                "Untitled template", {lt.template.name for lt in self._loaded}
            )
            new_filename = self._store.unique_filename_for(new_name, existing=existing)
            default_format = self._first_available_format_filename()
            self._editing = CompositeTemplate(
                name=new_name,
                format_filename=default_format,
                subrequests=[
                    Subrequest(
                        reference_id="firstStep",
                        method=HttpMethod.POST,
                        url="/services/data/v63.0/sobjects/Account",
                        body=[],
                    )
                ],
            )
            self._original = copy.deepcopy(self._editing)
            self._selected_filename = new_filename
            synthetic = LoadedTemplate(template=self._editing, filename=new_filename)
            self._loaded.append(synthetic)
            self._loaded.sort(key=lambda lt: lt.template.name.casefold())

            self._refresh_list_after_local_change(select_filename=new_filename)
            self._populate_editor()
            self._detail_stack.set_visible_child_name("editor")
            self._delete_btn.set_sensitive(False)
            self._update_dirty_state(force_dirty=True)

        self._maybe_discard_then(proceed)

    def _first_available_format_filename(self) -> str:
        try:
            formats = self._formats_store.list_formats()
        except Exception:
            log.exception("Could not list formats while creating template")
            return ""
        return formats[0].filename if formats else ""

    @staticmethod
    def _unique_display_name(base: str, existing_names: set[str]) -> str:
        if base not in existing_names:
            return base
        counter = 2
        while f"{base} {counter}" in existing_names:
            counter += 1
        return f"{base} {counter}"

    def _refresh_list_after_local_change(self, *, select_filename: str | None) -> None:
        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)
        for loaded in self._loaded:
            row = Adw.ActionRow()
            row.set_title(loaded.template.name)
            if loaded.template.description:
                row.set_subtitle(loaded.template.description)
            row.set_activatable(True)
            row._loaded = loaded  # type: ignore[attr-defined]  # noqa: SLF001
            self._list_box.append(row)
        self._sidebar_stack.set_visible_child_name("list" if self._loaded else "empty")
        if select_filename is not None:
            self._select_row_by_filename(select_filename)

    # ---------------------------------------------------------------- Editor
    def _populate_editor(self) -> None:
        if self._editing is None:
            return
        self._suppress_dirty = True
        try:
            self._name_row.set_text(self._editing.name)
            self._description_row.set_text(self._editing.description)
            self._all_or_none_row.set_active(self._editing.all_or_none)
            self._collate_row.set_active(self._editing.collate_subrequests)
            self._populate_format_combo()
            self._populate_subrequests()
        finally:
            self._suppress_dirty = False

    def _populate_format_combo(self) -> None:
        if self._editing is None:
            return
        try:
            available = self._formats_store.list_formats()
        except Exception:
            log.exception("Could not list formats")
            available = []

        self._format_filenames_in_combo = [lf.filename for lf in available]
        self._missing_format_inserted = False

        # Drain previous entries — Gtk.StringList has no clear method, so we
        # rebuild it.
        new_model = Gtk.StringList()
        for lf in available:
            new_model.append(lf.format.name)
        self._format_row.set_model(new_model)
        self._format_model = new_model

        target = self._editing.format_filename
        selected_index = -1
        if target:
            for index, filename in enumerate(self._format_filenames_in_combo):
                if filename == target:
                    selected_index = index
                    break
            if selected_index == -1:
                # Insert a leading "missing" entry.
                missing_model = Gtk.StringList()
                missing_model.append(f"⚠ Missing: {target}")
                for lf in available:
                    missing_model.append(lf.format.name)
                self._format_row.set_model(missing_model)
                self._format_model = missing_model
                self._format_filenames_in_combo = [target] + [lf.filename for lf in available]
                self._missing_format_inserted = True
                selected_index = 0
        if selected_index < 0 and self._format_filenames_in_combo:
            selected_index = 0
            self._editing.format_filename = self._format_filenames_in_combo[0]
        self._format_row.set_selected(max(selected_index, 0))

    def _resolve_linked_format(self) -> FileFormat | None:
        if self._editing is None or not self._editing.format_filename:
            return None
        try:
            loaded = self._formats_store.load(self._editing.format_filename)
        except Exception:
            log.exception("Could not load linked format")
            return None
        return loaded.format if loaded is not None else None

    def _populate_subrequests(self) -> None:
        # Walk the group and remove all rows we've added.
        for child in list(self._iter_subrequest_rows()):
            self._subrequests_group.remove(child)
        self._subrequest_expanders.clear()
        self._body_sections.clear()
        self._headers_sections.clear()

        if self._editing is None:
            self._update_subrequest_count()
            return
        for index, sub in enumerate(self._editing.subrequests):
            self._subrequests_group.add(self._build_subrequest_row(index, sub))
        self._update_subrequest_count()

    def _iter_subrequest_rows(self) -> list[Gtk.Widget]:
        rows: list[Gtk.Widget] = []
        # Adw.PreferencesGroup wraps its rows in a private container; the
        # public API for iterating is to walk children of the group itself.
        child = self._subrequests_group.get_first_child()
        # The actual rows are nested inside the group; do a generic walk and
        # collect any ``Adw.ExpanderRow``.
        stack: list[Gtk.Widget | None] = [child]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if isinstance(node, Adw.ExpanderRow):
                rows.append(node)
            sibling = node.get_next_sibling()
            if sibling is not None:
                stack.append(sibling)
            inner = node.get_first_child()
            if inner is not None:
                stack.append(inner)
        return rows

    def _update_subrequest_count(self) -> None:
        count = len(self._editing.subrequests) if self._editing is not None else 0
        self._subrequests_group.set_description(f"{count} of {MAX_SUBREQUESTS} subrequests.")
        self._add_sub_btn.set_sensitive(count < MAX_SUBREQUESTS)

    def _build_subrequest_row(self, index: int, sub: Subrequest) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow()
        expander.set_title(self._subrequest_title(index, sub))
        expander.set_subtitle(sub.url)
        expander.set_expanded(False)

        # Action buttons in the expander prefix area.
        def _on_up(_b: Gtk.Button) -> None:
            self._on_move_up(index)

        def _on_down(_b: Gtk.Button) -> None:
            self._on_move_down(index)

        def _on_dup(_b: Gtk.Button) -> None:
            self._on_duplicate(index)

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_subrequest(index)

        up_btn = Gtk.Button(icon_name="go-up-symbolic")
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text("Move up")
        up_btn.connect("clicked", _on_up)
        down_btn = Gtk.Button(icon_name="go-down-symbolic")
        down_btn.add_css_class("flat")
        down_btn.set_tooltip_text("Move down")
        down_btn.connect("clicked", _on_down)
        dup_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        dup_btn.add_css_class("flat")
        dup_btn.set_tooltip_text("Duplicate")
        dup_btn.connect("clicked", _on_dup)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text("Remove")
        del_btn.connect("clicked", _on_del)
        for btn in (up_btn, down_btn, dup_btn, del_btn):
            expander.add_suffix(btn)

        # Reference id row.
        ref_row = Adw.EntryRow()
        ref_row.set_title("Reference id")
        ref_row.set_text(sub.reference_id)

        def _on_ref_changed(entry: Adw.EntryRow, _spec: object) -> None:
            self._on_reference_id_changed(index, entry.get_text())

        ref_row.connect("notify::text", _on_ref_changed)
        expander.add_row(ref_row)

        # Method row.
        method_row = Adw.ComboRow()
        method_row.set_title("Method")
        method_model = Gtk.StringList.new([m.value for m in _HTTP_METHODS])
        method_row.set_model(method_model)
        method_row.set_selected(_HTTP_METHODS.index(sub.method))

        def _on_method_changed(combo: Adw.ComboRow, _spec: object) -> None:
            self._on_method_changed(index, combo.get_selected())

        method_row.connect("notify::selected", _on_method_changed)
        expander.add_row(method_row)

        # URL row.
        url_row = Adw.EntryRow()
        url_row.set_title("URL")
        url_row.set_text(sub.url)

        def _on_url_changed(entry: Adw.EntryRow, _spec: object) -> None:
            self._on_url_changed(index, entry.get_text())

        url_row.connect("notify::text", _on_url_changed)
        expander.add_row(url_row)

        # Body editor — list of typed key/value pairs.
        body_section = self._build_body_section(index, sub)
        expander.add_row(body_section)
        self._body_sections[index] = body_section

        # Headers editor — list of plain string key/value pairs.
        headers_section = self._build_headers_section(index, sub)
        expander.add_row(headers_section)
        self._headers_sections[index] = headers_section

        self._subrequest_expanders[index] = expander
        return expander

    @staticmethod
    def _subrequest_title(index: int, sub: Subrequest) -> str:
        ref = sub.reference_id or "(unnamed)"
        return f"#{index + 1} {ref} · {sub.method.value}"

    def _build_body_section(self, sub_index: int, sub: Subrequest) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_hexpand(True)

        title_label = Gtk.Label(label="Body", xalign=0)
        title_label.add_css_class("heading")
        outer.append(title_label)

        subtitle_label = Gtk.Label(
            label="Field/value pairs. Leave empty for no body.",
            xalign=0,
            wrap=True,
        )
        subtitle_label.add_css_class("dim-label")
        subtitle_label.add_css_class("caption")
        outer.append(subtitle_label)

        body_list = Gtk.ListBox()
        body_list.set_selection_mode(Gtk.SelectionMode.NONE)
        body_list.add_css_class("boxed-list")
        body_list.set_hexpand(True)

        body_entries = sub.body if sub.body is not None else []
        for entry_index, entry in enumerate(body_entries):
            body_list.append(self._build_body_field_row(sub_index, entry_index, entry))
        outer.append(body_list)

        add_field_btn = Gtk.Button(label="Add field", icon_name="list-add-symbolic")
        add_field_btn.add_css_class("flat")
        add_field_btn.set_halign(Gtk.Align.START)

        def _on_add_field(_b: Gtk.Button) -> None:
            self._on_add_body_field(sub_index)

        add_field_btn.connect("clicked", _on_add_field)
        outer.append(add_field_btn)

        row.set_child(outer)
        # Stash the inner list so we can rebuild only the rows on add/remove
        # without recreating the whole expander (which would collapse it).
        row._body_list = body_list  # type: ignore[attr-defined]  # noqa: SLF001
        return row

    def _build_body_field_row(
        self, sub_index: int, field_index: int, entry: BodyField
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_hexpand(True)

        field_entry = Gtk.Entry()
        field_entry.set_text(entry.field)
        field_entry.set_placeholder_text("Field")
        field_entry.set_hexpand(True)
        field_entry.set_size_request(-1, -1)

        value_entry = Gtk.Entry()
        value_entry.set_text(entry.value)
        value_entry.set_placeholder_text("Literal or {{column}}")
        value_entry.set_hexpand(True)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)

        def _on_field_changed(e: Gtk.Entry) -> None:
            self._on_body_field_changed(sub_index, field_index, e.get_text())

        def _on_value_changed(e: Gtk.Entry) -> None:
            self._on_body_value_changed(sub_index, field_index, e.get_text())

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_body_field(sub_index, field_index)

        field_entry.connect("changed", _on_field_changed)
        value_entry.connect("changed", _on_value_changed)
        del_btn.connect("clicked", _on_del)

        # 25 % field, 75 % value approximate split via Gtk.SizeGroup-equivalent
        # weighting: Gtk.Entry honours hexpand uniformly, so we set explicit
        # natural-width hints.
        field_entry.set_width_chars(12)
        value_entry.set_width_chars(36)

        box.append(field_entry)
        box.append(value_entry)
        box.append(del_btn)

        row.set_child(box)
        return row

    def _build_headers_section(self, sub_index: int, sub: Subrequest) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_hexpand(True)

        title_label = Gtk.Label(label="Headers", xalign=0)
        title_label.add_css_class("heading")
        outer.append(title_label)

        headers_list = Gtk.ListBox()
        headers_list.set_selection_mode(Gtk.SelectionMode.NONE)
        headers_list.add_css_class("boxed-list")
        headers_list.set_hexpand(True)
        for header_index, (key, value) in enumerate(sub.headers.items()):
            headers_list.append(self._build_header_row(sub_index, header_index, key, value))
        outer.append(headers_list)

        add_header_btn = Gtk.Button(label="Add header", icon_name="list-add-symbolic")
        add_header_btn.add_css_class("flat")
        add_header_btn.set_halign(Gtk.Align.START)

        def _on_add_header(_b: Gtk.Button) -> None:
            self._on_add_header(sub_index)

        add_header_btn.connect("clicked", _on_add_header)
        outer.append(add_header_btn)

        row.set_child(outer)
        row._headers_list = headers_list  # type: ignore[attr-defined]  # noqa: SLF001
        return row

    def _build_header_row(
        self, sub_index: int, header_index: int, key: str, value: str
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_hexpand(True)

        def _on_key_changed(entry: Gtk.Entry) -> None:
            self._on_header_key_changed(sub_index, header_index, entry)

        def _on_value_changed(entry: Gtk.Entry) -> None:
            self._on_header_value_changed(sub_index, header_index, entry)

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_header(sub_index, header_index)

        key_entry = Gtk.Entry()
        key_entry.set_text(key)
        key_entry.set_placeholder_text("Header name")
        key_entry.set_hexpand(True)
        key_entry.set_width_chars(12)
        key_entry.connect("changed", _on_key_changed)
        box.append(key_entry)

        value_entry = Gtk.Entry()
        value_entry.set_text(value)
        value_entry.set_placeholder_text("Header value")
        value_entry.set_hexpand(True)
        value_entry.set_width_chars(36)
        value_entry.connect("changed", _on_value_changed)
        box.append(value_entry)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.connect("clicked", _on_del)
        box.append(del_btn)

        row.set_child(box)
        return row

    # ----- Editor signal handlers
    def _on_name_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.name = self._name_row.get_text()
        self._update_dirty_state()

    def _on_description_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.description = self._description_row.get_text()
        self._update_dirty_state()

    def _on_format_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        index = self._format_row.get_selected()
        if 0 <= index < len(self._format_filenames_in_combo):
            self._editing.format_filename = self._format_filenames_in_combo[index]
            # If the user picked a real format, drop any "Missing" placeholder.
            if self._missing_format_inserted and index != 0:
                self._missing_format_inserted = False
                self._populate_format_combo()
        self._update_dirty_state()

    def _on_pick_another_format(self, _banner: Adw.Banner) -> None:
        # Best-effort focus jump: just open the combo via grab_focus on the row.
        self._format_row.grab_focus()

    def _on_all_or_none_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.all_or_none = self._all_or_none_row.get_active()
        self._update_dirty_state()

    def _on_collate_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.collate_subrequests = self._collate_row.get_active()
        self._update_dirty_state()

    # ----- Subrequest mutations
    def _on_add_subrequest(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        if len(self._editing.subrequests) >= MAX_SUBREQUESTS:
            return
        self._editing.subrequests.append(
            Subrequest(
                reference_id=f"step{len(self._editing.subrequests) + 1}",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/Account",
                body=None,
            )
        )
        self._populate_subrequests()
        self._update_dirty_state()

    def _on_remove_subrequest(self, index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= index < len(self._editing.subrequests)):
            return
        del self._editing.subrequests[index]
        self._populate_subrequests()
        self._update_dirty_state()

    def _on_duplicate(self, index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= index < len(self._editing.subrequests)):
            return
        original = self._editing.subrequests[index]
        # Deep copy so dicts/lists in body are independent.
        clone = copy.deepcopy(original)
        clone.reference_id = self._unique_reference_id(original.reference_id)
        self._editing.subrequests.insert(index + 1, clone)
        self._populate_subrequests()
        self._update_dirty_state()

    def _unique_reference_id(self, base: str) -> str:
        if self._editing is None:
            return base
        existing = {s.reference_id for s in self._editing.subrequests}
        candidate = f"{base}Copy" if base else "step"
        counter = 2
        while candidate in existing:
            candidate = f"{base}Copy{counter}" if base else f"step{counter}"
            counter += 1
        return candidate

    def _on_move_up(self, index: int) -> None:
        if self._editing is None or index <= 0:
            return
        subs = self._editing.subrequests
        subs[index - 1], subs[index] = subs[index], subs[index - 1]
        self._populate_subrequests()
        self._update_dirty_state()

    def _on_move_down(self, index: int) -> None:
        if self._editing is None:
            return
        subs = self._editing.subrequests
        if index >= len(subs) - 1:
            return
        subs[index + 1], subs[index] = subs[index], subs[index + 1]
        self._populate_subrequests()
        self._update_dirty_state()

    def _on_reference_id_changed(self, index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.subrequests)):
            return
        self._editing.subrequests[index].reference_id = value
        self._update_dirty_state()

    def _on_method_changed(self, index: int, selected: int) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.subrequests)):
            return
        if 0 <= selected < len(_HTTP_METHODS):
            self._editing.subrequests[index].method = _HTTP_METHODS[selected]
            self._update_dirty_state()

    def _on_url_changed(self, index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.subrequests)):
            return
        self._editing.subrequests[index].url = value
        self._update_dirty_state()

    def _on_add_body_field(self, sub_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        if sub.body is None:
            sub.body = []
        sub.body.append(BodyField(field="", value=""))
        self._refresh_body_list(sub_index)
        self._update_dirty_state()

    def _on_remove_body_field(self, sub_index: int, field_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        if sub.body is None:
            return
        if not (0 <= field_index < len(sub.body)):
            return
        del sub.body[field_index]
        if not sub.body:
            sub.body = None
        self._refresh_body_list(sub_index)
        self._update_dirty_state()

    def _refresh_body_list(self, sub_index: int) -> None:
        section = self._body_sections.get(sub_index)
        if section is None or self._editing is None:
            return
        body_list: Gtk.ListBox | None = getattr(section, "_body_list", None)
        if body_list is None:
            return
        self._suppress_dirty = True
        try:
            while True:
                child = body_list.get_first_child()
                if child is None:
                    break
                body_list.remove(child)
            sub = self._editing.subrequests[sub_index]
            entries = sub.body if sub.body is not None else []
            for entry_index, entry in enumerate(entries):
                body_list.append(self._build_body_field_row(sub_index, entry_index, entry))
        finally:
            self._suppress_dirty = False

    def _on_body_field_changed(self, sub_index: int, field_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        if sub.body is None or not (0 <= field_index < len(sub.body)):
            return
        sub.body[field_index].field = value
        self._update_dirty_state()

    def _on_body_value_changed(self, sub_index: int, field_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        if sub.body is None or not (0 <= field_index < len(sub.body)):
            return
        sub.body[field_index].value = value
        self._update_dirty_state()

    def _on_add_header(self, sub_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        if "" in sub.headers:
            # An empty-keyed header is already present; don't add another
            # (the dict can't hold two of them).
            return
        sub.headers[""] = ""
        self._refresh_headers_list(sub_index)
        self._update_dirty_state()

    def _refresh_headers_list(self, sub_index: int) -> None:
        section = self._headers_sections.get(sub_index)
        if section is None or self._editing is None:
            return
        headers_list: Gtk.ListBox | None = getattr(section, "_headers_list", None)
        if headers_list is None:
            return
        self._suppress_dirty = True
        try:
            while True:
                child = headers_list.get_first_child()
                if child is None:
                    break
                headers_list.remove(child)
            sub = self._editing.subrequests[sub_index]
            for header_index, (key, value) in enumerate(sub.headers.items()):
                headers_list.append(self._build_header_row(sub_index, header_index, key, value))
        finally:
            self._suppress_dirty = False

    def _on_header_key_changed(self, sub_index: int, header_index: int, entry: Gtk.Entry) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        items = list(sub.headers.items())
        if not (0 <= header_index < len(items)):
            return
        new_key = entry.get_text()
        old_key, old_value = items[header_index]
        if new_key == old_key:
            return
        # Rebuild the dict preserving insertion order.
        rebuilt: dict[str, str] = {}
        for current_index, (key, value) in enumerate(items):
            if current_index == header_index:
                rebuilt[new_key] = old_value
            else:
                rebuilt[key] = value
        sub.headers = rebuilt
        self._update_dirty_state()

    def _on_header_value_changed(self, sub_index: int, header_index: int, entry: Gtk.Entry) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        items = list(sub.headers.items())
        if not (0 <= header_index < len(items)):
            return
        key, _value = items[header_index]
        sub.headers[key] = entry.get_text()
        self._update_dirty_state()

    def _on_remove_header(self, sub_index: int, header_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= sub_index < len(self._editing.subrequests)):
            return
        sub = self._editing.subrequests[sub_index]
        items = list(sub.headers.items())
        if not (0 <= header_index < len(items)):
            return
        sub.headers = {k: v for i, (k, v) in enumerate(items) if i != header_index}
        self._refresh_headers_list(sub_index)
        self._update_dirty_state()

    # --------------------------------------------------------- Dirty / valid
    def _is_dirty(self) -> bool:
        if self._editing is None or self._original is None:
            return False
        return self._editing != self._original

    def _validate_template(self) -> tuple[bool, str | None]:
        """Return (is_valid, first_error_message)."""
        if self._editing is None:
            return False, "No template selected."
        fmt = self._resolve_linked_format()
        report = self._validator.validate(self._editing, fmt)
        if not report.ok:
            return False, report.errors[0].message
        # Slug uniqueness check across other templates.
        my_filename = self._selected_filename
        new_slug_filename = f"{slugify(self._editing.name)}.json"
        for loaded in self._loaded:
            if loaded.filename == my_filename:
                continue
            if loaded.filename == new_slug_filename:
                return (
                    False,
                    f"Name conflicts with another template: {loaded.template.name}.",
                )
        return True, None

    def _update_dirty_state(self, *, force_dirty: bool = False) -> None:
        dirty = force_dirty or self._is_dirty()
        is_valid, error = self._validate_template()
        save_ok = dirty and is_valid
        preview_ok = self._editing is not None and is_valid

        self._unsaved_banner.set_revealed(dirty)

        missing = (
            self._editing is not None
            and bool(self._editing.format_filename)
            and self._resolve_linked_format() is None
        )
        self._missing_banner.set_revealed(missing)

        self._save_btn.set_sensitive(save_ok)
        self._preview_btn.set_sensitive(preview_ok)

        run_ok, run_reason = self._run_sensitivity(is_valid, dirty, missing)
        self._run_btn.set_sensitive(run_ok)
        self._run_btn.set_tooltip_text(
            "Execute against active connection" if run_ok else (run_reason or "")
        )

        if save_ok or not dirty:
            self._save_btn.set_tooltip_text("")
        elif error:
            self._save_btn.set_tooltip_text(error)
        else:
            self._save_btn.set_tooltip_text("")

    def _run_sensitivity(
        self, is_valid: bool, dirty: bool, missing: bool
    ) -> tuple[bool, str | None]:
        if self._editing is None:
            return False, None
        if missing:
            return False, "Linked format is missing — pick one first."
        if not is_valid:
            return False, "Fix validation errors first."
        if dirty:
            return False, "Save the template before running."
        if self._get_active_alias() is None:
            return False, "No active connection — pick one in the sidebar."
        if self._is_run_in_progress():
            return False, "A run is in progress."
        return True, None

    def _is_run_in_progress(self) -> bool:
        return self._cancelled is not None and not self._cancelled.is_set()

    # ------------------------------------------------------------ Save flow
    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        is_valid, error = self._validate_template()
        if not is_valid:
            self._window.show_toast(error or "Invalid template.", timeout=6)
            return
        previous_on_disk = self._selected_filename if self._delete_btn.get_sensitive() else None
        try:
            new_filename = self._store.save(self._editing, previous_filename=previous_on_disk)
        except CompositeTemplateError as exc:
            self._window.show_toast(str(exc), timeout=6)
            return

        # Reload from disk so _loaded, _editing and _original all point to
        # fresh, independent deep copies. Reuse _switch_to_template to rebuild
        # the editor widgets, otherwise stale handlers from before the save
        # would keep mutating the old _editing instance.
        saved_name = self._editing.name
        self._selected_filename = new_filename
        self._delete_btn.set_sensitive(True)
        try:
            self._loaded = self._store.list_templates()
        except Exception:
            log.exception("Could not list templates after save")
            self._loaded = []
        self._refresh_list_after_local_change(select_filename=new_filename)
        self._switch_to_template(new_filename)
        self._window.show_toast(f"Saved “{saved_name}”.")

    # ---------------------------------------------------------- Delete flow
    def _on_delete_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        name = self._editing.name
        filename = self._selected_filename

        def do_delete() -> None:
            try:
                self._store.delete(filename)
            except CompositeTemplateError as exc:
                self._window.show_toast(str(exc), timeout=6)
                return
            self._selected_filename = None
            self._editing = None
            self._original = None
            self._refresh_list()
            self._show_empty_detail()
            self._window.show_toast(f"Deleted “{name}”.")

        confirm(
            self._window,
            heading=f"Delete “{name}”?",
            body="The template will be removed permanently from disk.",
            label="Delete",
            on_confirm=do_delete,
        )

    # --------------------------------------------- Unsaved-changes prompts
    def _maybe_discard_then(self, proceed: Callable[[], None]) -> None:
        if not self._is_dirty():
            proceed()
            return
        self._prompt_unsaved_changes(proceed=proceed)

    def _prompt_unsaved_changes(self, *, proceed: Callable[[], None]) -> None:
        dialog = Adw.AlertDialog(
            heading="Unsaved changes",
            body="The current template has unsaved edits. Save them before continuing?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "save":
                is_valid, error = self._validate_template()
                if not is_valid:
                    self._window.show_toast(error or "Invalid template.", timeout=6)
                    return
                self._on_save_clicked(self._save_btn)
                proceed()
            elif response == "discard":
                # If this was an unsaved synthetic new template, drop it.
                if self._selected_filename is not None and not self._delete_btn.get_sensitive():
                    self._loaded = [
                        lt for lt in self._loaded if lt.filename != self._selected_filename
                    ]
                if self._original is not None:
                    self._editing = copy.deepcopy(self._original)
                proceed()

        dialog.connect("response", on_response)
        dialog.present(self._window)

    # ---------------------------------------------------- Preview / Dry-run
    def _on_preview_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        self._render_preview(use_csv_path=None)
        self._detail_stack.set_visible_child_name("preview")

    def _on_back_to_editor(self, _btn: Gtk.Button) -> None:
        self._detail_stack.set_visible_child_name("editor")
        self._update_editor_chrome_visibility()
        self._update_dirty_state()

    def _on_sample_changed(self, *_args: object) -> None:
        if self._sample_dropdown.get_selected() == 0:
            self._render_preview(use_csv_path=None)
            return
        # "From CSV…" was picked.
        self._sample_dropdown.set_selected(0)  # reset to synthetic until file picked
        dialog = Gtk.FileDialog()
        dialog.set_title("Pick a CSV file for the sample row")
        home = Gio.File.new_for_path(str(Path.home()))
        dialog.set_initial_folder(home)

        def on_picked(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            path_str = file.get_path()
            if not path_str:
                return
            self._render_preview(use_csv_path=Path(path_str))

        dialog.open(self._window, None, on_picked)

    def _render_preview(self, *, use_csv_path: Path | None) -> None:
        if self._editing is None:
            return
        fmt = self._resolve_linked_format()
        if fmt is None:
            self._preview_summary.set_label("Linked format missing — pick a format first.")
            self._preview_textview.get_buffer().set_text("")
            return

        if use_csv_path is None:
            row = CompositePayloadRenderer.synthetic_row(fmt)
            source_label = "synthetic sample row"
        else:
            row = self._row_from_csv(fmt, use_csv_path)
            if row is None:
                self._window.show_toast(
                    f"Could not read sample row from {use_csv_path.name}", timeout=6
                )
                row = CompositePayloadRenderer.synthetic_row(fmt)
                source_label = "synthetic sample row (CSV unreadable)"
            else:
                source_label = f"first data row of {use_csv_path.name}"

        payload = self._renderer.render(self._editing, fmt, row)
        rendered = json.dumps(payload, indent=2)
        sub_count = len(self._editing.subrequests)
        self._preview_summary.set_label(
            f"Rendered with {source_label} — {sub_count} subrequest(s)."
        )
        self._preview_textview.get_buffer().set_text(rendered)

    @staticmethod
    def _row_from_csv(fmt: FileFormat, path: Path) -> RenderRow | None:
        try:
            text = path.read_text(encoding=fmt.encoding)
        except (OSError, UnicodeDecodeError):
            return None
        reader = csv.reader(
            text.splitlines(),
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
        )
        rows = list(reader)
        if not rows:
            return None
        if fmt.has_header and len(rows) >= 2:
            data_row = rows[1]
        elif fmt.has_header:
            return None
        else:
            data_row = rows[0]
        values: dict[str, str] = {}
        for column, raw in zip(fmt.columns, data_row, strict=False):
            values[column.name] = raw
        return RenderRow(values=values)

    # ---------------------------------------------------- External hooks
    def on_formats_changed(self) -> None:
        """Re-resolve the linked format and refresh the orphan banner."""
        if self._editing is None:
            return
        self._suppress_dirty = True
        try:
            self._populate_format_combo()
        finally:
            self._suppress_dirty = False
        self._update_dirty_state()

    def on_active_org_changed(self) -> None:
        """Refresh Run button sensitivity when the active connection changes."""
        if self._editing is None:
            return
        self._update_dirty_state()

    # ============================================================== Run flow
    def _on_run_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        alias = self._get_active_alias()
        fmt = self._resolve_linked_format()
        if alias is None or fmt is None:
            self._window.show_toast(
                "Cannot run — pick an active connection and a linked format.",
                timeout=6,
            )
            return

        dialog = Gtk.FileDialog()
        dialog.set_title("Pick the CSV file to run against")
        home = Gio.File.new_for_path(str(Path.home()))
        dialog.set_initial_folder(home)

        # Capture state for use in the picker callback.
        editing_snapshot = copy.deepcopy(self._editing)
        captured_alias = alias
        captured_fmt = fmt

        def on_picked(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            path_str = file.get_path()
            if not path_str:
                return
            csv_path = Path(path_str)
            self._on_run_csv_picked(csv_path, editing_snapshot, captured_fmt, captured_alias)

        dialog.open(self._window, None, on_picked)

    def _on_run_csv_picked(
        self,
        csv_path: Path,
        tpl: CompositeTemplate,
        fmt: FileFormat,
        alias: str,
    ) -> None:
        # Pre-count rows so we can both validate readability and show N in the
        # confirmation dialog.
        try:
            text = csv_path.read_text(encoding=fmt.encoding)
        except (OSError, UnicodeDecodeError) as exc:
            self._window.show_toast(
                f"Could not read CSV with the linked format settings — {exc}",
                timeout=6,
            )
            return
        reader = csv.reader(
            text.splitlines(),
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
        )
        all_rows = list(reader)
        data_count = len(all_rows) - (1 if fmt.has_header and all_rows else 0)
        if data_count <= 0:
            self._window.show_toast("CSV has no data rows.", timeout=6)
            return

        body = (
            f"This will write data to “{alias}” using {data_count} CSV row(s).\n\n"
            f"All-or-none: {'on' if tpl.all_or_none else 'off'}\n"
            f"Collate:     {'on' if tpl.collate_subrequests else 'off'}\n"
            f"Source:      {csv_path.name}"
        )
        dialog = Adw.AlertDialog(heading=f"Run “{tpl.name}”?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("run", "Run")
        dialog.set_response_appearance("run", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "run":
                return
            self._start_execution(tpl=tpl, fmt=fmt, csv_path=csv_path, alias=alias)

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _start_execution(
        self,
        *,
        tpl: CompositeTemplate,
        fmt: FileFormat,
        csv_path: Path,
        alias: str,
    ) -> None:
        self._cancelled = threading.Event()
        self._show_running_pane(tpl_name=tpl.name, alias=alias)
        self._update_dirty_state()

        def progress(event: ProgressEvent) -> None:
            GLib.idle_add(self._on_progress, event)

        def worker() -> None:
            try:
                with self._service.get_authenticated_client(alias) as sf_client:
                    report = self._executor.run(
                        tpl,
                        fmt,
                        csv_path,
                        sf_client,
                        on_progress=progress,
                        cancelled=self._cancelled
                        if self._cancelled is not None
                        else threading.Event(),
                    )
                GLib.idle_add(self._on_run_done, report, csv_path, fmt, tpl.name)
            except ExecutionError as exc:
                GLib.idle_add(self._on_run_fatal, str(exc))
            except ConnectionsError as exc:
                GLib.idle_add(self._on_run_fatal, f"Connection error: {exc}")
            except Exception as exc:
                log.exception("Unexpected execution failure")
                GLib.idle_add(self._on_run_fatal, f"Unexpected error: {exc}")

        threading.Thread(target=worker, daemon=True, name=f"composite-run-{alias}").start()

    def _show_running_pane(self, *, tpl_name: str, alias: str) -> None:
        self._run_title_label.set_label(f"Running “{tpl_name}” on {alias}")
        self._run_progress_label.set_label("Preparing…")
        self._run_progress_bar.set_fraction(0.0)
        self._run_last_error.set_visible(False)
        self._run_cancel_btn.set_sensitive(True)
        self._run_cancel_btn.set_label("Cancel")
        self._run_spinner.start()
        self._detail_stack.set_visible_child_name("running")
        self._update_editor_chrome_visibility()

    def _update_editor_chrome_visibility(self) -> None:
        """Swap header buttons + banners depending on the visible pane.

        The HeaderBar itself is always visible so nothing jumps around; we
        toggle individual button visibility so "Back to editor" lands in the
        same slot Preview occupies, and "Export failures CSV…" lands in
        Save's slot.
        """
        current = self._detail_stack.get_visible_child_name()
        is_results = current == "results"
        is_running = current == "running"
        editor_chrome = not (is_results or is_running)

        # Editor-side controls: visible everywhere except running/results.
        self._preview_btn.set_visible(editor_chrome)
        self._run_btn.set_visible(editor_chrome)
        self._save_btn.set_visible(editor_chrome)
        self._delete_btn.set_visible(editor_chrome)

        # Results-side controls: visible only on the results pane.
        self._results_back_btn.set_visible(is_results)
        self._export_btn.set_visible(is_results)

        if not editor_chrome:
            self._unsaved_banner.set_revealed(False)
            self._missing_banner.set_revealed(False)

    def _on_progress(self, event: ProgressEvent) -> bool:
        self._run_progress_label.set_label(f"Processing row {event.processed} of {event.total}…")
        if event.total > 0:
            self._run_progress_bar.set_fraction(event.processed / event.total)
        if event.last_result is not None and event.last_result.status == "failure":
            summary = event.last_result.error_summary or "Failure"
            self._run_last_error.set_label(f"Last error: {summary}")
            self._run_last_error.set_visible(True)
        return False

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        if self._cancelled is None:
            return
        self._cancelled.set()
        self._run_cancel_btn.set_sensitive(False)
        self._run_progress_label.set_label("Cancelling — finishing current row…")

    def _on_run_done(
        self,
        report: ExecutionReport,
        csv_path: Path,
        fmt: FileFormat,
        tpl_name: str,
    ) -> bool:
        self._cancelled = None
        self._run_spinner.stop()
        self._last_report = report
        self._last_report_csv_path = csv_path
        self._last_report_fmt = fmt
        self._last_report_template_name = tpl_name
        self._render_results(report)
        self._detail_stack.set_visible_child_name("results")
        self._update_editor_chrome_visibility()
        self._update_dirty_state()
        return False

    def _on_run_fatal(self, message: str) -> bool:
        self._cancelled = None
        self._run_spinner.stop()
        self._window.show_toast(message, timeout=8)
        self._detail_stack.set_visible_child_name("editor")
        self._update_editor_chrome_visibility()
        self._update_dirty_state()
        return False

    def _render_results(self, report: ExecutionReport) -> None:
        # Build banner.
        suffix = ", cancelled" if report.cancelled else ""
        title = (
            f"Run completed: {report.succeeded}/{report.total} succeeded, "
            f"{report.failed} failed{suffix}."
        )
        self._results_banner.set_title(title)
        self._results_banner.remove_css_class("confirm-warning")
        self._results_banner.remove_css_class("confirm-urgent")
        if report.cancelled and report.failed > 0:
            self._results_banner.add_css_class("confirm-urgent")
        elif report.failed > 0:
            self._results_banner.add_css_class("confirm-warning")
        self._results_banner.set_revealed(True)

        # Drain previous rows.
        while True:
            child = self._results_listbox.get_first_child()
            if child is None:
                break
            self._results_listbox.remove(child)

        for row in report.rows:
            self._results_listbox.append(self._build_result_row(row))

        self._export_btn.set_sensitive(report.failed > 0)

    @staticmethod
    def _result_icon(status: str) -> str:
        if status == "success":
            return "object-select-symbolic"
        if status == "cancelled":
            return "process-stop-symbolic"
        return "dialog-error-symbolic"

    def _build_result_row(self, row: RowResult) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow()
        expander.set_title(f"Row #{row.row_index + 1} — {row.status}")
        if row.error_summary:
            subtitle = row.error_summary
            if len(subtitle) > 200:
                subtitle = subtitle[:197] + "…"
            expander.set_subtitle(subtitle)
        icon = Gtk.Image.new_from_icon_name(self._result_icon(row.status))
        icon.set_valign(Gtk.Align.CENTER)
        expander.add_prefix(icon)

        for sub in row.subrequest_results:
            sub_row = Adw.ActionRow()
            sub_row.set_title(f"{sub.reference_id} — HTTP {sub.http_status}")
            if sub.errors:
                err = sub.errors[0]
                sub_row.set_subtitle(f"{err.error_code}: {err.message}")
            elif sub.body is not None:
                try:
                    snippet = json.dumps(sub.body)
                except (TypeError, ValueError):
                    snippet = repr(sub.body)
                if len(snippet) > 200:
                    snippet = snippet[:197] + "…"
                sub_row.set_subtitle(snippet)
            expander.add_row(sub_row)
        return expander

    def _on_export_failures_clicked(self, _btn: Gtk.Button) -> None:
        if (
            self._last_report is None
            or self._last_report_fmt is None
            or self._last_report.failed == 0
        ):
            return
        report = self._last_report
        fmt = self._last_report_fmt
        suggested = f"{slugify(self._last_report_template_name)}-failures.csv"
        dialog = Gtk.FileDialog()
        dialog.set_title("Export failures CSV")
        dialog.set_initial_name(suggested)

        def on_saved(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return
            path_str = file.get_path()
            if not path_str:
                return
            try:
                count = export_failures_csv(report, fmt, Path(path_str))
            except OSError as exc:
                self._window.show_toast(f"Could not export — {exc}", timeout=6)
                return
            self._window.show_toast(f"Exported {count} failed row(s) to {Path(path_str).name}.")

        dialog.save(self._window, None, on_saved)


__all__ = ["CompositeTemplatesPage"]
