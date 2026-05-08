"""File Formats page — CRUD on user-defined CSV file shapes."""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from gi.repository import Adw, Gio, GLib, Gtk

from salesforce_object_flow.core.formats import (
    SUPPORTED_ENCODINGS,
    Column,
    ColumnType,
    FileFormat,
    slugify,
)
from salesforce_object_flow.i18n import N_, _, ngettext
from salesforce_object_flow.pages.groups import PageGroup
from salesforce_object_flow.services.formats import (
    CellError,
    FileFormatError,
    FileFormatStore,
    FileFormatValidator,
    LoadedFormat,
    ValidationReport,
)
from salesforce_object_flow.ui.helpers import confirm

if TYPE_CHECKING:
    from salesforce_object_flow.window import MainWindow

log = logging.getLogger(__name__)


class FileFormatsPage:
    NAME: ClassVar[str] = "formats"
    TITLE: ClassVar[str] = N_("File Formats")
    ICON_NAME: ClassVar[str] = "document-text-symbolic"
    GROUP: ClassVar[PageGroup] = PageGroup.DATA_MODEL

    def __init__(
        self,
        window: MainWindow,
        store: FileFormatStore,
        validator: FileFormatValidator,
        on_formats_changed: Callable[[], None] | None = None,
    ) -> None:
        self._window = window
        self._store = store
        self._validator = validator
        self._on_formats_changed = on_formats_changed

        self._loaded: list[LoadedFormat] = []
        self._selected_filename: str | None = None
        self._editing: FileFormat | None = None
        self._original: FileFormat | None = None
        self._test_token: int = 0
        self._suppress_dirty: bool = False

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
        sidebar_header.set_title_widget(Adw.WindowTitle(title=_("Formats")))
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)

        new_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_btn.add_css_class("flat")
        new_btn.set_tooltip_text(_("Create a new format"))
        new_btn.connect("clicked", self._on_new_clicked)
        sidebar_header.pack_end(new_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text(_("Reload formats from disk"))
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
            title=_("No formats yet"),
            description=_("Create your first file format to describe a CSV shape."),
            icon_name="document-properties-symbolic",
        )
        empty_btn = Gtk.Button(label=_("New format"))
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", self._on_new_clicked)
        empty.set_child(empty_btn)
        self._sidebar_stack.add_named(empty, "empty")

        sidebar_toolbar.set_content(self._sidebar_stack)
        page = Adw.NavigationPage(title=_("Formats"))
        page.set_child(sidebar_toolbar)
        return page

    # ---------------------------------------------------------- Detail (right)
    def _build_content_page(self) -> Adw.NavigationPage:
        content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()
        content_header.set_show_title(False)
        content_header.set_show_start_title_buttons(False)
        content_header.set_show_end_title_buttons(False)

        self._test_btn = Gtk.Button(
            label=_("Test against file…"),
            icon_name="document-open-symbolic",
        )
        self._test_btn.add_css_class("flat")
        self._test_btn.set_sensitive(False)
        self._test_btn.connect("clicked", self._on_test_clicked)
        content_header.pack_start(self._test_btn)

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

        content_toolbar.add_top_bar(content_header)

        self._unsaved_banner = Adw.Banner(title=_("Unsaved changes"))
        self._unsaved_banner.add_css_class("confirm-warning")
        content_toolbar.add_top_bar(self._unsaved_banner)

        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self._detail_stack.add_named(
            self._make_status_page(
                title=_("Select or create a format"),
                description=_("Pick a format from the left, or click + to create a new one."),
                icon_name="document-properties-symbolic",
            ),
            "empty",
        )

        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_vexpand(True)
        editor_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(800)
        clamp.set_child(self._build_editor_body())
        editor_scroll.set_child(clamp)
        self._detail_stack.add_named(editor_scroll, "editor")

        self._detail_stack.add_named(self._build_test_results_pane(), "test_results")

        content_toolbar.set_content(self._detail_stack)
        page = Adw.NavigationPage(title=_("Detail"))
        page.set_child(content_toolbar)
        return page

    @staticmethod
    def _make_status_page(*, title: str, description: str, icon_name: str) -> Adw.StatusPage:
        return Adw.StatusPage(title=title, description=description, icon_name=icon_name)

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

        parsing = Adw.PreferencesGroup()
        parsing.set_title(_("Parsing"))

        self._delimiter_row = Adw.EntryRow()
        self._delimiter_row.set_title(_("Delimiter"))
        self._delimiter_row.connect("notify::text", self._on_delimiter_changed)
        parsing.add(self._delimiter_row)

        self._quote_row = Adw.EntryRow()
        self._quote_row.set_title(_("Quote character"))
        self._quote_row.connect("notify::text", self._on_quote_changed)
        parsing.add(self._quote_row)

        self._has_header_row = Adw.SwitchRow()
        self._has_header_row.set_title(_("Has header"))
        self._has_header_row.set_subtitle(_("First row contains column names."))
        self._has_header_row.connect("notify::active", self._on_has_header_changed)
        parsing.add(self._has_header_row)

        self._encoding_row = Adw.ComboRow()
        self._encoding_row.set_title(_("Encoding"))
        self._encoding_model = Gtk.StringList.new(list(SUPPORTED_ENCODINGS))
        self._encoding_row.set_model(self._encoding_model)
        self._encoding_row.connect("notify::selected", self._on_encoding_changed)
        parsing.add(self._encoding_row)

        body.append(parsing)

        columns_group = Adw.PreferencesGroup()
        columns_group.set_title(_("Columns"))
        add_column_btn = Gtk.Button(label=_("Add column"), icon_name="list-add-symbolic")
        add_column_btn.add_css_class("flat")
        add_column_btn.connect("clicked", self._on_add_column_clicked)
        columns_group.set_header_suffix(add_column_btn)

        self._columns_box = Gtk.ListBox()
        self._columns_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._columns_box.add_css_class("boxed-list")
        columns_group.add(self._columns_box)

        body.append(columns_group)

        self._editor_body = body
        return body

    # ----------------------------------------------------------- Test pane
    def _build_test_results_pane(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_btn = Gtk.Button(label=_("Back to editor"), icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", self._on_back_to_editor)
        top.append(back_btn)
        outer.append(top)

        self._test_summary_label = Gtk.Label(xalign=0, wrap=True)
        self._test_summary_label.add_css_class("title-3")
        outer.append(self._test_summary_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._test_results_group = Adw.PreferencesGroup()
        clamp = Adw.Clamp()
        clamp.set_maximum_size(800)
        clamp.set_child(self._test_results_group)
        scroll.set_child(clamp)
        outer.append(scroll)

        return outer

    # ------------------------------------------------------------- Sidebar list
    def _refresh_list(self) -> None:
        try:
            self._loaded = self._store.list_formats()
        except Exception as exc:
            log.exception("Could not list formats")
            self._window.show_toast(
                _("Could not list formats — {error}").format(error=exc), timeout=6
            )
            self._loaded = []

        # Clear and rebuild rows.
        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)

        for loaded in self._loaded:
            row = Adw.ActionRow()
            row.set_title(loaded.format.name)
            if loaded.format.description:
                row.set_subtitle(loaded.format.description)
            row.set_activatable(True)
            row._loaded = loaded  # type: ignore[attr-defined]  # noqa: SLF001
            self._list_box.append(row)

        self._sidebar_stack.set_visible_child_name("list" if self._loaded else "empty")

        # Restore selection if possible; otherwise show the empty detail state.
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
            loaded: LoadedFormat | None = getattr(row, "_loaded", None)
            if loaded is not None and loaded.filename == filename:
                self._suppress_dirty = True
                try:
                    self._list_box.select_row(row)
                finally:
                    self._suppress_dirty = False
                return
            index += 1

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._suppress_dirty:
            return
        if row is None:
            return
        loaded: LoadedFormat | None = getattr(row, "_loaded", None)
        if loaded is None:
            return
        if loaded.filename == self._selected_filename:
            return

        if self._is_dirty():
            previous = self._selected_filename
            self._suppress_dirty = True
            try:
                self._list_box.select_row(self._row_for_filename(previous))
            finally:
                self._suppress_dirty = False
            self._prompt_unsaved_changes(
                target_filename=loaded.filename,
                proceed=lambda: self._switch_to_format(loaded.filename),
            )
            return
        self._switch_to_format(loaded.filename)

    def _row_for_filename(self, filename: str | None) -> Gtk.ListBoxRow | None:
        if filename is None:
            return None
        index = 0
        while True:
            row = self._list_box.get_row_at_index(index)
            if row is None:
                return None
            loaded: LoadedFormat | None = getattr(row, "_loaded", None)
            if loaded is not None and loaded.filename == filename:
                return row
            index += 1

    def _switch_to_format(self, filename: str) -> None:
        loaded = next((lf for lf in self._loaded if lf.filename == filename), None)
        if loaded is None:
            return
        self._selected_filename = filename
        self._editing = copy.deepcopy(loaded.format)
        self._original = copy.deepcopy(loaded.format)
        self._populate_editor()
        self._detail_stack.set_visible_child_name("editor")
        self._delete_btn.set_sensitive(True)
        self._update_dirty_state()

    def _show_empty_detail(self) -> None:
        self._editing = None
        self._original = None
        self._delete_btn.set_sensitive(False)
        self._save_btn.set_sensitive(False)
        self._test_btn.set_sensitive(False)
        self._unsaved_banner.set_revealed(False)
        self._detail_stack.set_visible_child_name("empty")

    # ------------------------------------------------------------ New format
    def _on_new_clicked(self, _btn: Gtk.Button) -> None:
        def proceed() -> None:
            existing = {lf.filename for lf in self._loaded}
            new_name = self._unique_display_name(
                _("Untitled format"), {lf.format.name for lf in self._loaded}
            )
            new_filename = self._store.unique_filename_for(new_name, existing=existing)
            self._editing = FileFormat(name=new_name)
            # Treat the new format as already on disk (so dirty detection
            # only triggers if the user actually changes something).
            self._original = copy.deepcopy(self._editing)
            self._selected_filename = new_filename
            # Synthetic LoadedFormat — saved on first Save click.
            synthetic = LoadedFormat(format=self._editing, filename=new_filename)
            self._loaded.append(synthetic)
            self._loaded.sort(key=lambda lf: lf.format.name.casefold())

            self._refresh_list_after_local_change(select_filename=new_filename)
            self._populate_editor()
            self._detail_stack.set_visible_child_name("editor")
            self._delete_btn.set_sensitive(False)  # nothing on disk to delete yet
            self._update_dirty_state(force_dirty=True)

        self._maybe_discard_then(proceed)

    def _unique_display_name(self, base: str, existing_names: set[str]) -> str:
        if base not in existing_names:
            return base
        counter = 2
        while f"{base} {counter}" in existing_names:
            counter += 1
        return f"{base} {counter}"

    # ---------------------------------------------------------------- Editor
    def _populate_editor(self) -> None:
        if self._editing is None:
            return
        self._suppress_dirty = True
        try:
            self._name_row.set_text(self._editing.name)
            self._description_row.set_text(self._editing.description)
            self._delimiter_row.set_text(self._editing.delimiter)
            self._quote_row.set_text(self._editing.quote_char)
            self._has_header_row.set_active(self._editing.has_header)
            try:
                index = SUPPORTED_ENCODINGS.index(self._editing.encoding)
            except ValueError:
                index = 0
            self._encoding_row.set_selected(index)
            self._populate_columns()
        finally:
            self._suppress_dirty = False

    def _populate_columns(self) -> None:
        while True:
            row = self._columns_box.get_first_child()
            if row is None:
                break
            self._columns_box.remove(row)

        if self._editing is None:
            return
        for index, column in enumerate(self._editing.columns):
            self._columns_box.append(self._build_column_row(index, column))

    def _build_column_row(self, index: int, column: Column) -> Gtk.Widget:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        name_entry = Gtk.Entry()
        name_entry.set_text(column.name)
        name_entry.set_hexpand(True)
        name_entry.set_placeholder_text(_("Column name"))

        def _on_name_changed(entry: Gtk.Entry) -> None:
            self._on_column_name_changed(index, entry)

        name_entry.connect("changed", _on_name_changed)
        box.append(name_entry)

        type_dropdown = Gtk.DropDown.new_from_strings([t.value for t in ColumnType])
        type_dropdown.set_selected(list(ColumnType).index(column.type))

        def _on_type_changed(dd: Gtk.DropDown, _spec: object) -> None:
            self._on_column_type_changed(index, dd)

        type_dropdown.connect("notify::selected", _on_type_changed)
        box.append(type_dropdown)

        nullable_label = Gtk.Label(label=_("Nullable"))
        nullable_label.add_css_class("dim-label")
        box.append(nullable_label)
        nullable_switch = Gtk.Switch()
        nullable_switch.set_active(column.nullable)
        nullable_switch.set_valign(Gtk.Align.CENTER)

        def _on_nullable_changed(sw: Gtk.Switch, _spec: object) -> None:
            self._on_column_nullable_changed(index, sw)

        nullable_switch.connect("notify::active", _on_nullable_changed)
        box.append(nullable_switch)

        remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.set_tooltip_text(_("Remove column"))

        def _on_remove(_button: Gtk.Button) -> None:
            self._on_remove_column_clicked(index)

        remove_btn.connect("clicked", _on_remove)
        box.append(remove_btn)

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

    def _on_delimiter_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.delimiter = self._delimiter_row.get_text()
        self._update_dirty_state()

    def _on_quote_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.quote_char = self._quote_row.get_text()
        self._update_dirty_state()

    def _on_has_header_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        self._editing.has_header = self._has_header_row.get_active()
        self._update_dirty_state()

    def _on_encoding_changed(self, *_args: object) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        index = self._encoding_row.get_selected()
        if 0 <= index < len(SUPPORTED_ENCODINGS):
            self._editing.encoding = SUPPORTED_ENCODINGS[index]
            self._update_dirty_state()

    def _on_add_column_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        self._editing.columns.append(Column(name="", type=ColumnType.STRING, nullable=True))
        self._populate_columns()
        self._update_dirty_state()

    def _on_remove_column_clicked(self, index: int) -> None:
        if self._editing is None:
            return
        if 0 <= index < len(self._editing.columns):
            del self._editing.columns[index]
            self._populate_columns()
            self._update_dirty_state()

    def _on_column_name_changed(self, index: int, entry: Gtk.Entry) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if 0 <= index < len(self._editing.columns):
            existing = self._editing.columns[index]
            self._editing.columns[index] = Column(
                name=entry.get_text(),
                type=existing.type,
                nullable=existing.nullable,
            )
            self._update_dirty_state()

    def _on_column_type_changed(self, index: int, dropdown: Gtk.DropDown) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if 0 <= index < len(self._editing.columns):
            type_values = list(ColumnType)
            selected = dropdown.get_selected()
            if 0 <= selected < len(type_values):
                existing = self._editing.columns[index]
                self._editing.columns[index] = Column(
                    name=existing.name,
                    type=type_values[selected],
                    nullable=existing.nullable,
                )
                self._update_dirty_state()

    def _on_column_nullable_changed(self, index: int, switch: Gtk.Switch) -> None:
        if self._editing is None or self._suppress_dirty:
            return
        if 0 <= index < len(self._editing.columns):
            existing = self._editing.columns[index]
            self._editing.columns[index] = Column(
                name=existing.name,
                type=existing.type,
                nullable=switch.get_active(),
            )
            self._update_dirty_state()

    # --------------------------------------------------------- Dirty / valid
    def _is_dirty(self) -> bool:
        if self._editing is None or self._original is None:
            return False
        return self._editing != self._original

    def _validate_form(self) -> str | None:
        """Return an error message, or None if the form is valid."""
        if self._editing is None:
            return _("No format selected.")
        if not self._editing.name.strip():
            return _("Name is required.")
        if len(self._editing.delimiter) != 1:
            return _("Delimiter must be a single character.")
        if len(self._editing.quote_char) != 1:
            return _("Quote character must be a single character.")
        if self._editing.delimiter == self._editing.quote_char:
            return _("Delimiter and quote character must differ.")
        if self._editing.encoding not in SUPPORTED_ENCODINGS:
            return _("Encoding must be one of: {encodings}.").format(
                encodings=", ".join(SUPPORTED_ENCODINGS)
            )
        if not self._editing.columns:
            return _("At least one column is required.")
        names: set[str] = set()
        for column in self._editing.columns:
            if not column.name.strip():
                return _("All columns must have a name.")
            if column.name in names:
                return _("Duplicate column name: {name}.").format(name=column.name)
            names.add(column.name)
        # Slug uniqueness against other formats.
        my_filename = self._selected_filename
        new_slug_filename = f"{slugify(self._editing.name)}.json"
        for loaded in self._loaded:
            if loaded.filename == my_filename:
                continue
            if loaded.filename == new_slug_filename:
                return _("Name conflicts with another format: {name}.").format(
                    name=loaded.format.name
                )
        return None

    def _update_dirty_state(self, *, force_dirty: bool = False) -> None:
        dirty = force_dirty or self._is_dirty()
        error = self._validate_form()
        valid = error is None

        self._unsaved_banner.set_revealed(dirty)
        self._save_btn.set_sensitive(dirty and valid)
        self._test_btn.set_sensitive(self._editing is not None and valid)

        if error and dirty:
            self._save_btn.set_tooltip_text(error)
        else:
            self._save_btn.set_tooltip_text("")

    # ------------------------------------------------------------ Save flow
    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        error = self._validate_form()
        if error is not None:
            self._window.show_toast(error, timeout=6)
            return

        # Determine whether the previous filename existed on disk. For a brand
        # new format (synthetic local entry) we pass previous_filename=None.
        previous_on_disk = self._selected_filename if self._delete_btn.get_sensitive() else None
        try:
            new_filename = self._store.save(self._editing, previous_filename=previous_on_disk)
        except FileFormatError as exc:
            self._window.show_toast(str(exc), timeout=6)
            return

        self._selected_filename = new_filename
        self._original = copy.deepcopy(self._editing)
        self._delete_btn.set_sensitive(True)
        self._refresh_list()
        self._window.show_toast(_("Saved “{name}”.").format(name=self._editing.name))
        self._update_dirty_state()
        if self._on_formats_changed is not None:
            self._on_formats_changed()

    def _refresh_list_after_local_change(self, *, select_filename: str | None) -> None:
        # Rebuild rows from self._loaded without re-reading disk (used after
        # adding a synthetic in-memory entry on New).
        while True:
            row = self._list_box.get_first_child()
            if row is None:
                break
            self._list_box.remove(row)
        for loaded in self._loaded:
            row = Adw.ActionRow()
            row.set_title(loaded.format.name)
            if loaded.format.description:
                row.set_subtitle(loaded.format.description)
            row.set_activatable(True)
            row._loaded = loaded  # type: ignore[attr-defined]  # noqa: SLF001
            self._list_box.append(row)
        self._sidebar_stack.set_visible_child_name("list" if self._loaded else "empty")
        if select_filename is not None:
            self._select_row_by_filename(select_filename)

    # ---------------------------------------------------------- Delete flow
    def _on_delete_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None or self._selected_filename is None:
            return
        name = self._editing.name
        filename = self._selected_filename

        def do_delete() -> None:
            try:
                self._store.delete(filename)
            except FileFormatError as exc:
                self._window.show_toast(str(exc), timeout=6)
                return
            self._selected_filename = None
            self._editing = None
            self._original = None
            self._refresh_list()
            self._show_empty_detail()
            self._window.show_toast(_("Deleted “{name}”.").format(name=name))
            if self._on_formats_changed is not None:
                self._on_formats_changed()

        confirm(
            self._window,
            heading=_("Delete “{name}”?").format(name=name),
            body=_("The format definition will be removed permanently from disk."),
            label=_("Delete"),
            on_confirm=do_delete,
        )

    # --------------------------------------------- Unsaved-changes prompts
    def _maybe_discard_then(self, proceed: Callable[[], None]) -> None:
        if not self._is_dirty():
            proceed()
            return
        self._prompt_unsaved_changes(target_filename=None, proceed=proceed)

    def _prompt_unsaved_changes(
        self,
        *,
        target_filename: str | None,
        proceed: Callable[[], None],
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Unsaved changes"),
            body=_("The current format has unsaved edits. Save them before continuing?"),
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
                error = self._validate_form()
                if error is not None:
                    self._window.show_toast(error, timeout=6)
                    return
                self._on_save_clicked(self._save_btn)
                proceed()
            elif response == "discard":
                # Drop edits in memory.
                if self._original is not None:
                    self._editing = copy.deepcopy(self._original)
                proceed()
            # cancel: do nothing
            _ = target_filename  # reserved for future row-selection rollback

        dialog.connect("response", on_response)
        dialog.present(self._window)

    # ------------------------------------------------------------- Test flow
    def _on_test_clicked(self, _btn: Gtk.Button) -> None:
        if self._editing is None:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Pick a CSV file to validate"))
        home = Gio.File.new_for_path(str(Path.home()))
        dialog.set_initial_folder(home)

        def on_picked(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return  # user cancelled
            path_str = file.get_path()
            if not path_str:
                return
            self._run_test(Path(path_str))

        dialog.open(self._window, None, on_picked)

    def _run_test(self, path: Path) -> None:
        if self._editing is None:
            return
        token = self._test_token = self._test_token + 1
        snapshot = copy.deepcopy(self._editing)

        # Switch to test pane with a placeholder.
        self._test_summary_label.set_label(
            _("Validating {filename}…").format(filename=path.name)
        )
        self._clear_test_results()
        self._detail_stack.set_visible_child_name("test_results")

        def worker() -> None:
            report = self._validator.validate(snapshot, path)
            GLib.idle_add(self._on_test_done, token, path, report)

        threading.Thread(target=worker, daemon=True, name=f"format-test-{path.name}").start()

    def _on_test_done(self, token: int, path: Path, report: ValidationReport) -> bool:
        if token != self._test_token:
            return False
        self._render_test_results(path, report)
        return False

    def _clear_test_results(self) -> None:
        # Adw.PreferencesGroup doesn't expose a clear API; rebuild it.
        scroll = self._test_results_group.get_parent()
        if scroll is None:
            return
        # Replace by a fresh group inside the same clamp.
        clamp = scroll
        while clamp is not None and not isinstance(clamp, Adw.Clamp):
            clamp = clamp.get_parent()
        new_group = Adw.PreferencesGroup()
        if isinstance(clamp, Adw.Clamp):
            clamp.set_child(new_group)
        self._test_results_group = new_group

    def _render_test_results(self, path: Path, report: ValidationReport) -> None:
        self._clear_test_results()
        if report.fatal is not None:
            self._test_summary_label.set_label(
                _("Could not read {filename}").format(filename=path.name)
            )
            row = Adw.ActionRow()
            row.set_title(_("Fatal error"))
            row.set_subtitle(report.fatal)
            row.add_css_class("field-error")
            self._test_results_group.add(row)
            return

        n_rows = report.rows_examined
        if not report.errors:
            if report.truncated:
                template = ngettext(
                    "All {rows} row of {filename} valid (capped).",
                    "All {rows} rows of {filename} valid (capped).",
                    n_rows,
                )
            else:
                template = ngettext(
                    "All {rows} row of {filename} valid.",
                    "All {rows} rows of {filename} valid.",
                    n_rows,
                )
            self._test_summary_label.set_label(template.format(rows=n_rows, filename=path.name))
            return

        n_errors = len(report.errors)
        if report.truncated:
            template = ngettext(
                "{errors} error in first {rows} rows of {filename} (capped)",
                "{errors} errors in first {rows} rows of {filename} (capped)",
                n_errors,
            )
        else:
            template = ngettext(
                "{errors} error in first {rows} rows of {filename}",
                "{errors} errors in first {rows} rows of {filename}",
                n_errors,
            )
        self._test_summary_label.set_label(
            template.format(errors=n_errors, rows=n_rows, filename=path.name)
        )
        for err in report.errors:
            self._test_results_group.add(self._build_error_row(err))

    def _build_error_row(self, err: CellError) -> Adw.ActionRow:
        row = Adw.ActionRow()
        prefix = _("Row {row}").format(row=err.row)
        if err.column:
            prefix += f" · {err.column}"
        row.set_title(prefix)
        if err.value:
            row.set_subtitle(
                _("Value “{value}” — {message}").format(value=err.value, message=err.message)
            )
        else:
            row.set_subtitle(err.message)
        return row

    def _on_back_to_editor(self, _btn: Gtk.Button) -> None:
        self._detail_stack.set_visible_child_name("editor")
