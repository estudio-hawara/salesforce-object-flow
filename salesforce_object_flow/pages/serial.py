"""Serial Requests page — CRUD on client-driven sequences of REST steps.

Sister page to :mod:`pages.composite`: same NavigationSplitView skeleton,
same dirty-tracking + three-way confirm flow. The fundamental difference is
that the executor is a *client-side* serial loop rather than a single
``/composite`` payload, which is why each step carries an optional
:class:`StepCondition` deciding whether it runs against the prior step's
results, plus a ``continue_on_failure`` flag.
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

from salesforce_object_flow.core.formats import FileFormat, slugify
from salesforce_object_flow.core.serial import (
    BodyField,
    CheckOp,
    ConditionCheck,
    ConditionCombinator,
    HttpMethod,
    SerialDefinition,
    SerialStep,
    StepCondition,
)
from salesforce_object_flow.i18n import N_, _, ngettext
from salesforce_object_flow.i18n_errors import format_error
from salesforce_object_flow.pages.groups import PageGroup
from salesforce_object_flow.services.connections import ConnectionsError, ConnectionsService
from salesforce_object_flow.services.formats import FileFormatStore
from salesforce_object_flow.services.serial import (
    ExecutionError,
    ExecutionReport,
    LoadedDefinition,
    ProgressEvent,
    RenderRow,
    RowResult,
    SerialDefinitionError,
    SerialDefinitionStore,
    SerialDefinitionValidator,
    SerialExecutor,
    SerialStepRenderer,
    StepResult,
    export_failures_csv,
)
from salesforce_object_flow.ui.helpers import confirm

if TYPE_CHECKING:
    from salesforce_object_flow.window import MainWindow

log = logging.getLogger(__name__)

_HTTP_METHODS: tuple[HttpMethod, ...] = tuple(HttpMethod)
_CHECK_OPS: tuple[CheckOp, ...] = tuple(CheckOp)
_COMBINATORS: tuple[ConditionCombinator, ...] = tuple(ConditionCombinator)


class SerialRequestsPage:
    NAME: ClassVar[str] = "serial"
    TITLE: ClassVar[str] = N_("Serial Requests")
    ICON_NAME: ClassVar[str] = "media-playlist-consecutive-symbolic"
    GROUP: ClassVar[PageGroup] = PageGroup.RUN

    def __init__(
        self,
        window: MainWindow,
        store: SerialDefinitionStore,
        validator: SerialDefinitionValidator,
        renderer: SerialStepRenderer,
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
        self._executor = SerialExecutor(renderer=renderer)

        self._loaded: list[LoadedDefinition] = []
        self._selected_filename: str | None = None
        self._editing: SerialDefinition | None = None
        self._original: SerialDefinition | None = None
        self._suppress_dirty: bool = False
        self._format_filenames_in_combo: list[str] = []
        self._missing_format_inserted: bool = False
        self._step_expanders: dict[int, Adw.ExpanderRow] = {}
        self._body_sections: dict[int, Gtk.ListBoxRow] = {}
        self._headers_sections: dict[int, Gtk.ListBoxRow] = {}
        self._condition_sections: dict[int, Gtk.ListBoxRow] = {}

        self._cancelled: threading.Event | None = None
        self._last_report: ExecutionReport | None = None
        self._last_report_csv_path: Path | None = None
        self._last_report_fmt: FileFormat | None = None
        self._last_report_definition_name: str = ""

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
        sidebar_header.set_title_widget(Adw.WindowTitle(title=_("Definitions")))
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)

        new_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_btn.add_css_class("flat")
        new_btn.set_tooltip_text(_("Create a new serial definition"))
        new_btn.connect("clicked", self._on_new_clicked)
        sidebar_header.pack_end(new_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text(_("Reload definitions from disk"))
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
            title=_("No serial definitions yet"),
            description=_("Create your first definition to compose conditional flows."),
            icon_name="document-properties-symbolic",
        )
        empty_btn = Gtk.Button(label=_("New definition"))
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", self._on_new_clicked)
        empty.set_child(empty_btn)
        self._sidebar_stack.add_named(empty, "empty")

        sidebar_toolbar.set_content(self._sidebar_stack)
        page = Adw.NavigationPage(title=_("Definitions"))
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
            label=_("Preview steps"),
            icon_name="view-reveal-symbolic",
        )
        self._preview_btn.add_css_class("flat")
        self._preview_btn.set_sensitive(False)
        self._preview_btn.connect("clicked", self._on_preview_clicked)
        content_header.pack_start(self._preview_btn)

        self._run_btn = Gtk.Button(
            label=_("Run…"),
            icon_name="media-playback-start-symbolic",
        )
        self._run_btn.add_css_class("flat")
        self._run_btn.set_sensitive(False)
        self._run_btn.set_tooltip_text(_("Execute against active connection"))
        self._run_btn.connect("clicked", self._on_run_clicked)
        content_header.pack_start(self._run_btn)

        self._results_back_btn = Gtk.Button(
            label=_("Back to editor"), icon_name="go-previous-symbolic"
        )
        self._results_back_btn.add_css_class("flat")
        self._results_back_btn.set_visible(False)
        self._results_back_btn.connect("clicked", self._on_back_to_editor)
        content_header.pack_start(self._results_back_btn)

        self._save_btn = Gtk.Button(label=_("Save"))
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save_clicked)
        content_header.pack_end(self._save_btn)

        self._delete_btn = Gtk.Button(label=_("Delete"))
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)
        content_header.pack_end(self._delete_btn)

        self._export_btn = Gtk.Button(
            label=_("Export failures CSV…"), icon_name="document-save-as-symbolic"
        )
        self._export_btn.add_css_class("flat")
        self._export_btn.set_visible(False)
        self._export_btn.connect("clicked", self._on_export_failures_clicked)
        content_header.pack_end(self._export_btn)

        content_toolbar.add_top_bar(content_header)

        self._missing_banner = Adw.Banner(title=_("Linked format not found"))
        self._missing_banner.add_css_class("confirm-urgent")
        self._missing_banner.set_button_label(_("Pick another format"))
        self._missing_banner.connect("button-clicked", self._on_pick_another_format)
        content_toolbar.add_top_bar(self._missing_banner)

        self._unsaved_banner = Adw.Banner(title=_("Unsaved changes"))
        self._unsaved_banner.add_css_class("confirm-warning")
        content_toolbar.add_top_bar(self._unsaved_banner)

        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        empty_status = Adw.StatusPage(
            title=_("Select or create a definition"),
            description=_("Pick one on the left, or click + to create a new one."),
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
        page = Adw.NavigationPage(title=_("Detail"))
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
        general.set_title(_("General"))
        self._name_row = Adw.EntryRow()
        self._name_row.set_title(_("Name"))
        self._name_row.connect("notify::text", self._on_name_changed)
        general.add(self._name_row)

        self._description_row = Adw.EntryRow()
        self._description_row.set_title(_("Description"))
        self._description_row.connect("notify::text", self._on_description_changed)
        general.add(self._description_row)
        body.append(general)

        linkage = Adw.PreferencesGroup()
        linkage.set_title(_("Linkage"))
        self._format_row = Adw.ComboRow()
        self._format_row.set_title(_("File format"))
        self._format_row.set_subtitle(
            _("Columns of this format drive {{placeholder}} substitution.")
        )
        self._format_model = Gtk.StringList()
        self._format_row.set_model(self._format_model)
        self._format_row.connect("notify::selected", self._on_format_changed)
        linkage.add(self._format_row)
        body.append(linkage)

        self._steps_group = Adw.PreferencesGroup()
        self._steps_group.set_title(_("Steps"))
        self._add_step_btn = Gtk.Button(label=_("Add step"), icon_name="list-add-symbolic")
        self._add_step_btn.add_css_class("flat")
        self._add_step_btn.connect("clicked", self._on_add_step)
        self._steps_group.set_header_suffix(self._add_step_btn)
        body.append(self._steps_group)

        self._editor_body = body
        return body

    def _build_preview_pane(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        back_btn = Gtk.Button(label=_("Back to editor"), icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", self._on_back_to_editor)
        top.append(back_btn)

        sample_label = Gtk.Label(label=_("Sample row:"))
        sample_label.add_css_class("dim-label")
        sample_label.set_valign(Gtk.Align.CENTER)
        top.append(sample_label)

        self._sample_dropdown = Gtk.DropDown.new_from_strings([_("Synthetic"), _("From CSV…")])
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

        self._run_cancel_btn = Gtk.Button(label=_("Cancel"))
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
            self._loaded = self._store.list_definitions()
        except Exception as exc:
            log.exception("Could not list serial definitions")
            self._window.show_toast(
                _("Could not list definitions — {error}").format(error=exc), timeout=6
            )
            self._loaded = []

        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)

        for loaded in self._loaded:
            row = Adw.ActionRow()
            row.set_title(loaded.definition.name)
            if loaded.definition.description:
                row.set_subtitle(loaded.definition.description)
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
            loaded: LoadedDefinition | None = getattr(row, "_loaded", None)
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
            loaded: LoadedDefinition | None = getattr(row, "_loaded", None)
            if loaded is not None and loaded.filename == filename:
                return row
            index += 1

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._suppress_dirty:
            return
        if row is None:
            return
        loaded: LoadedDefinition | None = getattr(row, "_loaded", None)
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
                _("A run is in progress — cancel it before switching definitions."),
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
                proceed=lambda: self._switch_to_definition(loaded.filename),
            )
            return
        self._switch_to_definition(loaded.filename)

    def _switch_to_definition(self, filename: str) -> None:
        loaded = next((ld for ld in self._loaded if ld.filename == filename), None)
        if loaded is None:
            return
        self._selected_filename = filename
        self._editing = copy.deepcopy(loaded.definition)
        self._original = copy.deepcopy(loaded.definition)
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

    # ------------------------------------------------------------ New definition
    def _on_new_clicked(self, _btn: Gtk.Button) -> None:
        def proceed() -> None:
            existing = {ld.filename for ld in self._loaded}
            new_name = self._unique_display_name(
                _("Untitled definition"), {ld.definition.name for ld in self._loaded}
            )
            new_filename = self._store.unique_filename_for(new_name, existing=existing)
            default_format = self._first_available_format_filename()
            self._editing = SerialDefinition(
                name=new_name,
                format_filename=default_format,
                steps=[
                    SerialStep(
                        reference_id="firstStep",
                        method=HttpMethod.GET,
                        url="/services/data/v63.0/query/?q=SELECT+Id+FROM+Contact",
                        body=None,
                    )
                ],
            )
            self._original = copy.deepcopy(self._editing)
            self._selected_filename = new_filename
            synthetic = LoadedDefinition(definition=self._editing, filename=new_filename)
            self._loaded.append(synthetic)
            self._loaded.sort(key=lambda ld: ld.definition.name.casefold())

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
            log.exception("Could not list formats while creating definition")
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
            row.set_title(loaded.definition.name)
            if loaded.definition.description:
                row.set_subtitle(loaded.definition.description)
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
            self._populate_format_combo()
            self._populate_steps()
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
                missing_model = Gtk.StringList()
                missing_model.append(_("⚠ Missing: {filename}").format(filename=target))
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

    def _populate_steps(self) -> None:
        for child in list(self._iter_step_rows()):
            self._steps_group.remove(child)
        self._step_expanders.clear()
        self._body_sections.clear()
        self._headers_sections.clear()
        self._condition_sections.clear()

        if self._editing is None:
            self._update_step_count()
            return
        for index, step in enumerate(self._editing.steps):
            self._steps_group.add(self._build_step_row(index, step))
        self._update_step_count()

    def _iter_step_rows(self) -> list[Gtk.Widget]:
        rows: list[Gtk.Widget] = []
        child = self._steps_group.get_first_child()
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

    def _update_step_count(self) -> None:
        count = len(self._editing.steps) if self._editing is not None else 0
        self._steps_group.set_description(
            ngettext("{count} step.", "{count} steps.", count).format(count=count)
        )

    def _build_step_row(self, index: int, step: SerialStep) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow()
        expander.set_title(self._step_title(index, step))
        expander.set_subtitle(step.url)
        expander.set_expanded(False)

        def _on_up(_b: Gtk.Button) -> None:
            self._on_move_up(index)

        def _on_down(_b: Gtk.Button) -> None:
            self._on_move_down(index)

        def _on_dup(_b: Gtk.Button) -> None:
            self._on_duplicate(index)

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_step(index)

        up_btn = Gtk.Button(icon_name="go-up-symbolic")
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text(_("Move up"))
        up_btn.connect("clicked", _on_up)
        down_btn = Gtk.Button(icon_name="go-down-symbolic")
        down_btn.add_css_class("flat")
        down_btn.set_tooltip_text(_("Move down"))
        down_btn.connect("clicked", _on_down)
        dup_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        dup_btn.add_css_class("flat")
        dup_btn.set_tooltip_text(_("Duplicate"))
        dup_btn.connect("clicked", _on_dup)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text(_("Remove"))
        del_btn.connect("clicked", _on_del)
        for btn in (up_btn, down_btn, dup_btn, del_btn):
            expander.add_suffix(btn)

        ref_row = Adw.EntryRow()
        ref_row.set_title(_("Reference id"))
        ref_row.set_text(step.reference_id)

        def _on_ref_changed(entry: Adw.EntryRow, _spec: object) -> None:
            self._on_reference_id_changed(index, entry.get_text())

        ref_row.connect("notify::text", _on_ref_changed)
        expander.add_row(ref_row)

        method_row = Adw.ComboRow()
        method_row.set_title(_("Method"))
        method_model = Gtk.StringList.new([m.value for m in _HTTP_METHODS])
        method_row.set_model(method_model)
        method_row.set_selected(_HTTP_METHODS.index(step.method))

        def _on_method_changed(combo: Adw.ComboRow, _spec: object) -> None:
            self._on_method_changed(index, combo.get_selected())

        method_row.connect("notify::selected", _on_method_changed)
        expander.add_row(method_row)

        url_row = Adw.EntryRow()
        url_row.set_title(_("URL"))
        url_row.set_text(step.url)

        def _on_url_changed(entry: Adw.EntryRow, _spec: object) -> None:
            self._on_url_changed(index, entry.get_text())

        url_row.connect("notify::text", _on_url_changed)
        expander.add_row(url_row)

        body_section = self._build_body_section(index, step)
        expander.add_row(body_section)
        self._body_sections[index] = body_section

        headers_section = self._build_headers_section(index, step)
        expander.add_row(headers_section)
        self._headers_sections[index] = headers_section

        condition_section = self._build_condition_section(index, step)
        expander.add_row(condition_section)
        self._condition_sections[index] = condition_section

        cof_row = Adw.SwitchRow()
        cof_row.set_title(_("Continue on failure"))
        cof_row.set_subtitle(_("Keep running later steps even if this one fails (HTTP ≥ 400)."))
        cof_row.set_active(step.continue_on_failure)

        def _on_cof_changed(row: Adw.SwitchRow, _spec: object) -> None:
            self._on_continue_on_failure_changed(index, row.get_active())

        cof_row.connect("notify::active", _on_cof_changed)
        expander.add_row(cof_row)

        self._step_expanders[index] = expander
        return expander

    @staticmethod
    def _step_title(index: int, step: SerialStep) -> str:
        ref = step.reference_id or _("(unnamed)")
        return f"#{index + 1} {ref} · {step.method.value}"

    def _build_body_section(self, step_index: int, step: SerialStep) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_hexpand(True)

        title_label = Gtk.Label(label=_("Body"), xalign=0)
        title_label.add_css_class("heading")
        outer.append(title_label)

        subtitle_label = Gtk.Label(
            label=_("Field/value pairs. Leave empty for no body."),
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

        body_entries = step.body if step.body is not None else []
        for entry_index, entry in enumerate(body_entries):
            body_list.append(self._build_body_field_row(step_index, entry_index, entry))
        outer.append(body_list)

        add_field_btn = Gtk.Button(label=_("Add field"), icon_name="list-add-symbolic")
        add_field_btn.add_css_class("flat")
        add_field_btn.set_halign(Gtk.Align.START)

        def _on_add_field(_b: Gtk.Button) -> None:
            self._on_add_body_field(step_index)

        add_field_btn.connect("clicked", _on_add_field)
        outer.append(add_field_btn)

        row.set_child(outer)
        row._body_list = body_list  # type: ignore[attr-defined]  # noqa: SLF001
        return row

    def _build_body_field_row(
        self, step_index: int, field_index: int, entry: BodyField
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
        field_entry.set_placeholder_text(_("Field"))
        field_entry.set_hexpand(True)

        value_entry = Gtk.Entry()
        value_entry.set_text(entry.value)
        value_entry.set_placeholder_text(_("Literal, {{column}} or @{ref.path}"))
        value_entry.set_hexpand(True)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)

        def _on_field_changed(e: Gtk.Entry) -> None:
            self._on_body_field_changed(step_index, field_index, e.get_text())

        def _on_value_changed(e: Gtk.Entry) -> None:
            self._on_body_value_changed(step_index, field_index, e.get_text())

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_body_field(step_index, field_index)

        field_entry.connect("changed", _on_field_changed)
        value_entry.connect("changed", _on_value_changed)
        del_btn.connect("clicked", _on_del)

        field_entry.set_width_chars(12)
        value_entry.set_width_chars(36)

        box.append(field_entry)
        box.append(value_entry)
        box.append(del_btn)

        row.set_child(box)
        return row

    def _build_headers_section(self, step_index: int, step: SerialStep) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_hexpand(True)

        title_label = Gtk.Label(label=_("Headers"), xalign=0)
        title_label.add_css_class("heading")
        outer.append(title_label)

        headers_list = Gtk.ListBox()
        headers_list.set_selection_mode(Gtk.SelectionMode.NONE)
        headers_list.add_css_class("boxed-list")
        headers_list.set_hexpand(True)
        for header_index, (key, value) in enumerate(step.headers.items()):
            headers_list.append(self._build_header_row(step_index, header_index, key, value))
        outer.append(headers_list)

        add_header_btn = Gtk.Button(label=_("Add header"), icon_name="list-add-symbolic")
        add_header_btn.add_css_class("flat")
        add_header_btn.set_halign(Gtk.Align.START)

        def _on_add_header(_b: Gtk.Button) -> None:
            self._on_add_header(step_index)

        add_header_btn.connect("clicked", _on_add_header)
        outer.append(add_header_btn)

        row.set_child(outer)
        row._headers_list = headers_list  # type: ignore[attr-defined]  # noqa: SLF001
        return row

    def _build_header_row(
        self, step_index: int, header_index: int, key: str, value: str
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_hexpand(True)

        def _on_key_changed(entry: Gtk.Entry) -> None:
            self._on_header_key_changed(step_index, header_index, entry)

        def _on_value_changed(entry: Gtk.Entry) -> None:
            self._on_header_value_changed(step_index, header_index, entry)

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_header(step_index, header_index)

        key_entry = Gtk.Entry()
        key_entry.set_text(key)
        key_entry.set_placeholder_text(_("Header name"))
        key_entry.set_hexpand(True)
        key_entry.set_width_chars(12)
        key_entry.connect("changed", _on_key_changed)
        box.append(key_entry)

        value_entry = Gtk.Entry()
        value_entry.set_text(value)
        value_entry.set_placeholder_text(_("Header value"))
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

    # ----- Condition editor
    def _build_condition_section(self, step_index: int, step: SerialStep) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_hexpand(True)

        title_label = Gtk.Label(label=_("Run when"), xalign=0)
        title_label.add_css_class("heading")
        outer.append(title_label)

        subtitle_label = Gtk.Label(
            label=_("Predicates on prior steps' results. Empty list ⇒ always run."),
            xalign=0,
            wrap=True,
        )
        subtitle_label.add_css_class("dim-label")
        subtitle_label.add_css_class("caption")
        outer.append(subtitle_label)

        combinator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        combinator_box.append(Gtk.Label(label=_("Combine with:"), xalign=0))
        combo = Gtk.DropDown.new_from_strings([_("All of (AND)"), _("Any of (OR)")])
        condition = step.condition
        if condition is None or condition.combinator is ConditionCombinator.ALL_OF:
            combo.set_selected(0)
        else:
            combo.set_selected(1)

        def _on_combinator_changed(d: Gtk.DropDown, _spec: object) -> None:
            self._on_combinator_changed(step_index, d.get_selected())

        combo.connect("notify::selected", _on_combinator_changed)
        combinator_box.append(combo)
        outer.append(combinator_box)

        checks_list = Gtk.ListBox()
        checks_list.set_selection_mode(Gtk.SelectionMode.NONE)
        checks_list.add_css_class("boxed-list")
        checks_list.set_hexpand(True)

        checks = condition.checks if condition is not None else []
        prev_refs = self._refs_before(step_index)
        for check_index, check in enumerate(checks):
            checks_list.append(self._build_check_row(step_index, check_index, check, prev_refs))
        outer.append(checks_list)

        add_check_btn = Gtk.Button(label=_("Add condition"), icon_name="list-add-symbolic")
        add_check_btn.add_css_class("flat")
        add_check_btn.set_halign(Gtk.Align.START)
        # Disable when there's no previous step to reference.
        add_check_btn.set_sensitive(bool(prev_refs))

        def _on_add_check(_b: Gtk.Button) -> None:
            self._on_add_check(step_index)

        add_check_btn.connect("clicked", _on_add_check)
        outer.append(add_check_btn)

        row.set_child(outer)
        row._checks_list = checks_list  # type: ignore[attr-defined]  # noqa: SLF001
        row._add_check_btn = add_check_btn  # type: ignore[attr-defined]  # noqa: SLF001
        return row

    def _refs_before(self, step_index: int) -> list[str]:
        if self._editing is None:
            return []
        return [s.reference_id for s in self._editing.steps[:step_index] if s.reference_id]

    def _build_check_row(
        self,
        step_index: int,
        check_index: int,
        check: ConditionCheck,
        available_refs: list[str],
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_hexpand(True)

        ref_options = available_refs if available_refs else [check.ref or "—"]
        ref_combo = Gtk.DropDown.new_from_strings(ref_options)
        if check.ref in ref_options:
            ref_combo.set_selected(ref_options.index(check.ref))
        else:
            ref_combo.set_selected(0)

        def _on_ref_changed(d: Gtk.DropDown, _spec: object) -> None:
            selected = d.get_selected()
            if 0 <= selected < len(ref_options):
                self._on_check_ref_changed(step_index, check_index, ref_options[selected])

        ref_combo.connect("notify::selected", _on_ref_changed)
        ref_combo.set_size_request(140, -1)
        box.append(ref_combo)

        op_options = [op.value for op in _CHECK_OPS]
        op_combo = Gtk.DropDown.new_from_strings(op_options)
        op_combo.set_selected(_CHECK_OPS.index(check.op))

        def _on_op_changed(d: Gtk.DropDown, _spec: object) -> None:
            self._on_check_op_changed(step_index, check_index, d.get_selected())

        op_combo.connect("notify::selected", _on_op_changed)
        op_combo.set_size_request(170, -1)
        box.append(op_combo)

        path_entry = Gtk.Entry()
        path_entry.set_text(check.path)
        path_entry.set_placeholder_text(_("Path (e.g. records[0].Id)"))
        path_entry.set_hexpand(True)
        path_entry.set_width_chars(18)

        def _on_path_changed(e: Gtk.Entry) -> None:
            self._on_check_path_changed(step_index, check_index, e.get_text())

        path_entry.connect("changed", _on_path_changed)
        box.append(path_entry)

        value_entry = Gtk.Entry()
        value_entry.set_text(check.value)
        value_entry.set_placeholder_text(_("Value"))
        value_entry.set_hexpand(True)
        value_entry.set_width_chars(10)

        def _on_value_changed(e: Gtk.Entry) -> None:
            self._on_check_value_changed(step_index, check_index, e.get_text())

        value_entry.connect("changed", _on_value_changed)
        box.append(value_entry)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")

        def _on_del(_b: Gtk.Button) -> None:
            self._on_remove_check(step_index, check_index)

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
            if self._missing_format_inserted and index != 0:
                self._missing_format_inserted = False
                self._populate_format_combo()
        self._update_dirty_state()

    def _on_pick_another_format(self, _banner: Adw.Banner) -> None:
        self._format_row.grab_focus()

    # ----- Step mutations
    def _on_add_step(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        self._editing.steps.append(
            SerialStep(
                reference_id=f"step{len(self._editing.steps) + 1}",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/Account",
                body=None,
            )
        )
        self._populate_steps()
        self._update_dirty_state()

    def _on_remove_step(self, index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        del self._editing.steps[index]
        self._populate_steps()
        self._update_dirty_state()

    def _on_duplicate(self, index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        original = self._editing.steps[index]
        clone = copy.deepcopy(original)
        clone.reference_id = self._unique_reference_id(original.reference_id)
        self._editing.steps.insert(index + 1, clone)
        self._populate_steps()
        self._update_dirty_state()

    def _unique_reference_id(self, base: str) -> str:
        if self._editing is None:
            return base
        existing = {s.reference_id for s in self._editing.steps}
        candidate = f"{base}Copy" if base else "step"
        counter = 2
        while candidate in existing:
            candidate = f"{base}Copy{counter}" if base else f"step{counter}"
            counter += 1
        return candidate

    def _on_move_up(self, index: int) -> None:
        if self._editing is None or index <= 0:
            return
        steps = self._editing.steps
        steps[index - 1], steps[index] = steps[index], steps[index - 1]
        self._populate_steps()
        self._update_dirty_state()

    def _on_move_down(self, index: int) -> None:
        if self._editing is None:
            return
        steps = self._editing.steps
        if index >= len(steps) - 1:
            return
        steps[index + 1], steps[index] = steps[index], steps[index + 1]
        self._populate_steps()
        self._update_dirty_state()

    def _on_reference_id_changed(self, index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        self._editing.steps[index].reference_id = value
        self._update_dirty_state()

    def _on_method_changed(self, index: int, selected: int) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        if 0 <= selected < len(_HTTP_METHODS):
            self._editing.steps[index].method = _HTTP_METHODS[selected]
            self._update_dirty_state()

    def _on_url_changed(self, index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        self._editing.steps[index].url = value
        self._update_dirty_state()

    def _on_continue_on_failure_changed(self, index: int, value: bool) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= index < len(self._editing.steps)):
            return
        self._editing.steps[index].continue_on_failure = value
        self._update_dirty_state()

    def _on_add_body_field(self, step_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.body is None:
            step.body = []
        step.body.append(BodyField(field="", value=""))
        self._refresh_body_list(step_index)
        self._update_dirty_state()

    def _on_remove_body_field(self, step_index: int, field_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.body is None:
            return
        if not (0 <= field_index < len(step.body)):
            return
        del step.body[field_index]
        if not step.body:
            step.body = None
        self._refresh_body_list(step_index)
        self._update_dirty_state()

    def _refresh_body_list(self, step_index: int) -> None:
        section = self._body_sections.get(step_index)
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
            step = self._editing.steps[step_index]
            entries = step.body if step.body is not None else []
            for entry_index, entry in enumerate(entries):
                body_list.append(self._build_body_field_row(step_index, entry_index, entry))
        finally:
            self._suppress_dirty = False

    def _on_body_field_changed(self, step_index: int, field_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.body is None or not (0 <= field_index < len(step.body)):
            return
        step.body[field_index].field = value
        self._update_dirty_state()

    def _on_body_value_changed(self, step_index: int, field_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.body is None or not (0 <= field_index < len(step.body)):
            return
        step.body[field_index].value = value
        self._update_dirty_state()

    def _on_add_header(self, step_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if "" in step.headers:
            return
        step.headers[""] = ""
        self._refresh_headers_list(step_index)
        self._update_dirty_state()

    def _refresh_headers_list(self, step_index: int) -> None:
        section = self._headers_sections.get(step_index)
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
            step = self._editing.steps[step_index]
            for header_index, (key, value) in enumerate(step.headers.items()):
                headers_list.append(self._build_header_row(step_index, header_index, key, value))
        finally:
            self._suppress_dirty = False

    def _on_header_key_changed(self, step_index: int, header_index: int, entry: Gtk.Entry) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        items = list(step.headers.items())
        if not (0 <= header_index < len(items)):
            return
        new_key = entry.get_text()
        old_key, _old_value = items[header_index]
        if new_key == old_key:
            return
        rebuilt: dict[str, str] = {}
        for current_index, (key, value) in enumerate(items):
            if current_index == header_index:
                rebuilt[new_key] = value
            else:
                rebuilt[key] = value
        step.headers = rebuilt
        self._update_dirty_state()

    def _on_header_value_changed(
        self, step_index: int, header_index: int, entry: Gtk.Entry
    ) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        items = list(step.headers.items())
        if not (0 <= header_index < len(items)):
            return
        key, _value = items[header_index]
        step.headers[key] = entry.get_text()
        self._update_dirty_state()

    def _on_remove_header(self, step_index: int, header_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        items = list(step.headers.items())
        if not (0 <= header_index < len(items)):
            return
        step.headers = {k: v for i, (k, v) in enumerate(items) if i != header_index}
        self._refresh_headers_list(step_index)
        self._update_dirty_state()

    # ----- Condition mutations
    def _on_combinator_changed(self, step_index: int, selected: int) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.condition is None:
            step.condition = StepCondition()
        if 0 <= selected < len(_COMBINATORS):
            step.condition.combinator = _COMBINATORS[selected]
        else:
            step.condition.combinator = ConditionCombinator.ALL_OF
        self._update_dirty_state()

    def _on_add_check(self, step_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        prev_refs = self._refs_before(step_index)
        if not prev_refs:
            self._window.show_toast(
                _("No previous step to reference — add at least one step before this."),
                timeout=4,
            )
            return
        step = self._editing.steps[step_index]
        if step.condition is None:
            step.condition = StepCondition()
        step.condition.checks.append(ConditionCheck(op=CheckOp.STATUS_OK, ref=prev_refs[0]))
        self._refresh_checks_list(step_index)
        self._update_dirty_state()

    def _on_remove_check(self, step_index: int, check_index: int) -> None:
        if self._editing is None:
            return
        if not (0 <= step_index < len(self._editing.steps)):
            return
        step = self._editing.steps[step_index]
        if step.condition is None:
            return
        if not (0 <= check_index < len(step.condition.checks)):
            return
        del step.condition.checks[check_index]
        if not step.condition.checks:
            # Reset to default (always run) when last check is removed.
            step.condition = None
        self._refresh_checks_list(step_index)
        self._update_dirty_state()

    def _refresh_checks_list(self, step_index: int) -> None:
        section = self._condition_sections.get(step_index)
        if section is None or self._editing is None:
            return
        checks_list: Gtk.ListBox | None = getattr(section, "_checks_list", None)
        add_btn: Gtk.Button | None = getattr(section, "_add_check_btn", None)
        if checks_list is None:
            return
        self._suppress_dirty = True
        try:
            while True:
                child = checks_list.get_first_child()
                if child is None:
                    break
                checks_list.remove(child)
            step = self._editing.steps[step_index]
            prev_refs = self._refs_before(step_index)
            if add_btn is not None:
                add_btn.set_sensitive(bool(prev_refs))
            checks = step.condition.checks if step.condition is not None else []
            for check_index, check in enumerate(checks):
                checks_list.append(self._build_check_row(step_index, check_index, check, prev_refs))
        finally:
            self._suppress_dirty = False

    def _on_check_ref_changed(self, step_index: int, check_index: int, ref: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        check = self._get_check(step_index, check_index)
        if check is None:
            return
        check.ref = ref
        self._update_dirty_state()

    def _on_check_op_changed(self, step_index: int, check_index: int, selected: int) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        check = self._get_check(step_index, check_index)
        if check is None:
            return
        if 0 <= selected < len(_CHECK_OPS):
            check.op = _CHECK_OPS[selected]
            self._update_dirty_state()

    def _on_check_path_changed(self, step_index: int, check_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        check = self._get_check(step_index, check_index)
        if check is None:
            return
        check.path = value
        self._update_dirty_state()

    def _on_check_value_changed(self, step_index: int, check_index: int, value: str) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        check = self._get_check(step_index, check_index)
        if check is None:
            return
        check.value = value
        self._update_dirty_state()

    def _get_check(self, step_index: int, check_index: int) -> ConditionCheck | None:
        if self._editing is None:
            return None
        if not (0 <= step_index < len(self._editing.steps)):
            return None
        step = self._editing.steps[step_index]
        if step.condition is None:
            return None
        if not (0 <= check_index < len(step.condition.checks)):
            return None
        return step.condition.checks[check_index]

    # --------------------------------------------------------- Dirty / valid
    def _is_dirty(self) -> bool:
        if self._editing is None or self._original is None:
            return False
        return self._editing != self._original

    def _validate_definition(self) -> tuple[bool, str | None]:
        if self._editing is None:
            return False, _("No definition selected.")
        fmt = self._resolve_linked_format()
        report = self._validator.validate(self._editing, fmt)
        if not report.ok:
            return False, report.errors[0].message
        my_filename = self._selected_filename
        new_slug_filename = f"{slugify(self._editing.name)}.json"
        for loaded in self._loaded:
            if loaded.filename == my_filename:
                continue
            if loaded.filename == new_slug_filename:
                return (
                    False,
                    _("Name conflicts with another definition: {name}.").format(
                        name=loaded.definition.name
                    ),
                )
        return True, None

    def _update_dirty_state(self, *, force_dirty: bool = False) -> None:
        dirty = force_dirty or self._is_dirty()
        is_valid, error = self._validate_definition()
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
            _("Execute against active connection") if run_ok else (run_reason or "")
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
            return False, _("Linked format is missing — pick one first.")
        if not is_valid:
            return False, _("Fix validation errors first.")
        if dirty:
            return False, _("Save the definition before running.")
        if self._get_active_alias() is None:
            return False, _("No active connection — pick one in the sidebar.")
        if self._is_run_in_progress():
            return False, _("A run is in progress.")
        return True, None

    def _is_run_in_progress(self) -> bool:
        return self._cancelled is not None and not self._cancelled.is_set()

    # ------------------------------------------------------------ Save flow
    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        is_valid, error = self._validate_definition()
        if not is_valid:
            self._window.show_toast(error or _("Invalid definition."), timeout=6)
            return
        previous_on_disk = self._selected_filename if self._delete_btn.get_sensitive() else None
        try:
            new_filename = self._store.save(self._editing, previous_filename=previous_on_disk)
        except SerialDefinitionError as exc:
            self._window.show_toast(format_error(exc), timeout=6)
            return

        saved_name = self._editing.name
        self._selected_filename = new_filename
        self._delete_btn.set_sensitive(True)
        try:
            self._loaded = self._store.list_definitions()
        except Exception:
            log.exception("Could not list definitions after save")
            self._loaded = []
        self._refresh_list_after_local_change(select_filename=new_filename)
        self._switch_to_definition(new_filename)
        self._window.show_toast(_("Saved “{name}”.").format(name=saved_name))

    def _on_delete_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        name = self._editing.name
        filename = self._selected_filename

        def do_delete() -> None:
            try:
                self._store.delete(filename)
            except SerialDefinitionError as exc:
                self._window.show_toast(format_error(exc), timeout=6)
                return
            self._selected_filename = None
            self._editing = None
            self._original = None
            self._refresh_list()
            self._show_empty_detail()
            self._window.show_toast(_("Deleted “{name}”.").format(name=name))

        confirm(
            self._window,
            heading=_("Delete “{name}”?").format(name=name),
            body=_("The definition will be removed permanently from disk."),
            label=_("Delete"),
            on_confirm=do_delete,
        )

    def _maybe_discard_then(self, proceed: Callable[[], None]) -> None:
        if not self._is_dirty():
            proceed()
            return
        self._prompt_unsaved_changes(proceed=proceed)

    def _prompt_unsaved_changes(self, *, proceed: Callable[[], None]) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Unsaved changes"),
            body=_("The current definition has unsaved edits. Save them before continuing?"),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("discard", _("Discard"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "save":
                is_valid, error = self._validate_definition()
                if not is_valid:
                    self._window.show_toast(error or _("Invalid definition."), timeout=6)
                    return
                self._on_save_clicked(self._save_btn)
                proceed()
            elif response == "discard":
                if self._selected_filename is not None and not self._delete_btn.get_sensitive():
                    self._loaded = [
                        ld for ld in self._loaded if ld.filename != self._selected_filename
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
        self._sample_dropdown.set_selected(0)
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Pick a CSV file for the sample row"))
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
            self._preview_summary.set_label(_("Linked format missing — pick a format first."))
            self._preview_textview.get_buffer().set_text("")
            return

        if use_csv_path is None:
            row = SerialStepRenderer.synthetic_row(fmt)
            source_label = _("synthetic sample row")
        else:
            row = self._row_from_csv(fmt, use_csv_path)
            if row is None:
                self._window.show_toast(
                    _("Could not read sample row from {filename}").format(
                        filename=use_csv_path.name
                    ),
                    timeout=6,
                )
                row = SerialStepRenderer.synthetic_row(fmt)
                source_label = _("synthetic sample row (CSV unreadable)")
            else:
                source_label = _("first data row of {filename}").format(filename=use_csv_path.name)

        # Build a descriptive preview: each step shown with its condition
        # and the rendered request body. Conditions are not actually
        # evaluated against a real run, so we describe them textually.
        chunks: list[str] = []
        prior_empty: dict[str, StepResult] = {}
        for index, step in enumerate(self._editing.steps):
            try:
                rendered = self._renderer.render_step(step, fmt, row, prior_empty)
            except Exception as exc:  # defensive: render of preview should not crash UI
                log.exception("Preview render failed for step %s", step.reference_id)
                chunks.append(f"# Step #{index + 1} {step.reference_id} — render error: {exc}")
                continue
            condition_line = self._describe_condition(step)
            request_block = {
                "method": rendered.method.value,
                "url": rendered.url,
            }
            if rendered.body is not None:
                request_block["body"] = rendered.body  # type: ignore[assignment]
            if rendered.headers:
                request_block["headers"] = rendered.headers  # type: ignore[assignment]
            if step.continue_on_failure:
                request_block["continue_on_failure"] = True  # type: ignore[assignment]
            header = (
                f"# Step #{index + 1} · {step.reference_id} · {step.method.value}\n"
                f"#   {condition_line}"
            )
            chunks.append(header + "\n" + json.dumps(request_block, indent=2))
        rendered_text = "\n\n".join(chunks) if chunks else _("No steps to preview.")
        step_count = len(self._editing.steps)
        self._preview_summary.set_label(
            ngettext(
                "Rendered with {source} — {count} step.",
                "Rendered with {source} — {count} steps.",
                step_count,
            ).format(source=source_label, count=step_count)
        )
        self._preview_textview.get_buffer().set_text(rendered_text)

    @staticmethod
    def _describe_condition(step: SerialStep) -> str:
        condition = step.condition
        if condition is None or not condition.checks:
            return _("Run when: always.")
        joiner = _(" AND ") if condition.combinator is ConditionCombinator.ALL_OF else _(" OR ")
        parts: list[str] = []
        for check in condition.checks:
            if check.op in (CheckOp.STATUS_OK, CheckOp.STATUS_FAILED):
                parts.append(f"{check.op.value}({check.ref})")
            elif check.op in (CheckOp.EXISTS, CheckOp.NOT_EXISTS):
                parts.append(f"{check.op.value}(@{{{check.ref}.{check.path}}})")
            else:
                parts.append(f"@{{{check.ref}.{check.path}}} {check.op.value} {check.value}")
        return _("Run when: ") + joiner.join(parts)

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
        if self._editing is None:
            return
        self._suppress_dirty = True
        try:
            self._populate_format_combo()
        finally:
            self._suppress_dirty = False
        self._update_dirty_state()

    def on_active_org_changed(self) -> None:
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
                _("Cannot run — pick an active connection and a linked format."),
                timeout=6,
            )
            return

        dialog = Gtk.FileDialog()
        dialog.set_title(_("Pick the CSV file to run against"))
        home = Gio.File.new_for_path(str(Path.home()))
        dialog.set_initial_folder(home)

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
        definition: SerialDefinition,
        fmt: FileFormat,
        alias: str,
    ) -> None:
        try:
            text = csv_path.read_text(encoding=fmt.encoding)
        except (OSError, UnicodeDecodeError) as exc:
            self._window.show_toast(
                _("Could not read CSV with the linked format settings — {error}").format(error=exc),
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
            self._window.show_toast(_("CSV has no data rows."), timeout=6)
            return

        intro = ngettext(
            "This will write data to “{alias}” using {count} CSV row.",
            "This will write data to “{alias}” using {count} CSV rows.",
            data_count,
        ).format(alias=alias, count=data_count)
        body = (
            f"{intro}\n\n"
            + _("Steps: {count}").format(count=len(definition.steps))
            + "\n"
            + _("Source: {filename}").format(filename=csv_path.name)
        )
        dialog = Adw.AlertDialog(heading=_("Run “{name}”?").format(name=definition.name), body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("run", _("Run"))
        dialog.set_response_appearance("run", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "run":
                return
            self._start_execution(definition=definition, fmt=fmt, csv_path=csv_path, alias=alias)

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _start_execution(
        self,
        *,
        definition: SerialDefinition,
        fmt: FileFormat,
        csv_path: Path,
        alias: str,
    ) -> None:
        self._cancelled = threading.Event()
        self._show_running_pane(definition_name=definition.name, alias=alias)
        self._update_dirty_state()

        def progress(event: ProgressEvent) -> None:
            GLib.idle_add(self._on_progress, event)

        def worker() -> None:
            try:
                with self._service.get_authenticated_client(alias) as sf_client:
                    report = self._executor.run(
                        definition,
                        fmt,
                        csv_path,
                        sf_client,
                        on_progress=progress,
                        cancelled=self._cancelled
                        if self._cancelled is not None
                        else threading.Event(),
                    )
                GLib.idle_add(self._on_run_done, report, csv_path, fmt, definition.name)
            except ExecutionError as exc:
                GLib.idle_add(self._on_run_fatal, format_error(exc))
            except ConnectionsError as exc:
                GLib.idle_add(self._on_run_fatal, format_error(exc))
            except Exception as exc:
                log.exception("Unexpected serial execution failure")
                GLib.idle_add(
                    self._on_run_fatal,
                    _("Unexpected error: {error}").format(error=exc),
                )

        threading.Thread(target=worker, daemon=True, name=f"serial-run-{alias}").start()

    def _show_running_pane(self, *, definition_name: str, alias: str) -> None:
        self._run_title_label.set_label(
            _("Running “{name}” on {alias}").format(name=definition_name, alias=alias)
        )
        self._run_progress_label.set_label(_("Preparing…"))
        self._run_progress_bar.set_fraction(0.0)
        self._run_last_error.set_visible(False)
        self._run_cancel_btn.set_sensitive(True)
        self._run_cancel_btn.set_label(_("Cancel"))
        self._run_spinner.start()
        self._detail_stack.set_visible_child_name("running")
        self._update_editor_chrome_visibility()

    def _update_editor_chrome_visibility(self) -> None:
        current = self._detail_stack.get_visible_child_name()
        is_results = current == "results"
        is_running = current == "running"
        editor_chrome = not (is_results or is_running)

        self._preview_btn.set_visible(editor_chrome)
        self._run_btn.set_visible(editor_chrome)
        self._save_btn.set_visible(editor_chrome)
        self._delete_btn.set_visible(editor_chrome)

        self._results_back_btn.set_visible(is_results)
        self._export_btn.set_visible(is_results)

        if not editor_chrome:
            self._unsaved_banner.set_revealed(False)
            self._missing_banner.set_revealed(False)

    def _on_progress(self, event: ProgressEvent) -> bool:
        self._run_progress_label.set_label(
            _("Processing row {processed} of {total}…").format(
                processed=event.processed, total=event.total
            )
        )
        if event.total > 0:
            self._run_progress_bar.set_fraction(event.processed / event.total)
        if event.last_result is not None and event.last_result.status == "failure":
            summary = event.last_result.error_summary or _("Failure")
            self._run_last_error.set_label(_("Last error: {summary}").format(summary=summary))
            self._run_last_error.set_visible(True)
        return False

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        if self._cancelled is None:
            return
        self._cancelled.set()
        self._run_cancel_btn.set_sensitive(False)
        self._run_progress_label.set_label(_("Cancelling — finishing current row…"))

    def _on_run_done(
        self,
        report: ExecutionReport,
        csv_path: Path,
        fmt: FileFormat,
        definition_name: str,
    ) -> bool:
        self._cancelled = None
        self._run_spinner.stop()
        self._last_report = report
        self._last_report_csv_path = csv_path
        self._last_report_fmt = fmt
        self._last_report_definition_name = definition_name
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
        if report.cancelled:
            title = _(
                "Run completed: {succeeded}/{total} succeeded, {failed} failed, cancelled."
            ).format(succeeded=report.succeeded, total=report.total, failed=report.failed)
        else:
            title = _("Run completed: {succeeded}/{total} succeeded, {failed} failed.").format(
                succeeded=report.succeeded, total=report.total, failed=report.failed
            )
        self._results_banner.set_title(title)
        self._results_banner.remove_css_class("confirm-warning")
        self._results_banner.remove_css_class("confirm-urgent")
        if report.cancelled and report.failed > 0:
            self._results_banner.add_css_class("confirm-urgent")
        elif report.failed > 0:
            self._results_banner.add_css_class("confirm-warning")
        self._results_banner.set_revealed(True)

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

    @staticmethod
    def _step_status_icon(status: str) -> str:
        if status == "success":
            return "object-select-symbolic"
        if status == "skipped":
            return "media-playback-pause-symbolic"
        return "dialog-error-symbolic"

    def _build_result_row(self, row: RowResult) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow()
        expander.set_title(
            _("Row #{number} — {status}").format(number=row.row_index + 1, status=row.status)
        )
        if row.error_summary:
            subtitle = row.error_summary
            if len(subtitle) > 200:
                subtitle = subtitle[:197] + "…"
            expander.set_subtitle(subtitle)
        icon = Gtk.Image.new_from_icon_name(self._result_icon(row.status))
        icon.set_valign(Gtk.Align.CENTER)
        expander.add_prefix(icon)

        for step in row.step_results:
            sub_row = Adw.ActionRow()
            label = f"{step.reference_id} — {step.status}"
            if step.http_status:
                label += f" (HTTP {step.http_status})"
            sub_row.set_title(label)
            if step.error_summary:
                sub_row.set_subtitle(step.error_summary)
            elif step.body is not None:
                try:
                    snippet = json.dumps(step.body)
                except (TypeError, ValueError):
                    snippet = repr(step.body)
                if len(snippet) > 200:
                    snippet = snippet[:197] + "…"
                sub_row.set_subtitle(snippet)
            sub_icon = Gtk.Image.new_from_icon_name(self._step_status_icon(step.status))
            sub_icon.set_valign(Gtk.Align.CENTER)
            sub_row.add_prefix(sub_icon)
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
        suggested = f"{slugify(self._last_report_definition_name)}-failures.csv"
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Export failures CSV"))
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
                self._window.show_toast(
                    _("Could not export — {error}").format(error=exc), timeout=6
                )
                return
            self._window.show_toast(
                ngettext(
                    "Exported {count} failed row to {filename}.",
                    "Exported {count} failed rows to {filename}.",
                    count,
                ).format(count=count, filename=Path(path_str).name)
            )

        dialog.save(self._window, None, on_saved)


__all__ = ["SerialRequestsPage"]
