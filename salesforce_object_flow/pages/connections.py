"""Connections page — list, add, re-auth, test, and remove Salesforce connections."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Final

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from salesforce_object_flow.core.config import DEFAULT_API_VERSION, OrgEntry
from salesforce_object_flow.services.connections import (
    AddOrgRequest,
    ConnectionsError,
    ConnectionsService,
    ProgressEvent,
)
from salesforce_object_flow.services.oauth import CALLBACK_URL
from salesforce_object_flow.ui.helpers import confirm
from salesforce_object_flow.ui.layout import make_page_layout

if TYPE_CHECKING:
    from salesforce_object_flow.window import MainWindow

log = logging.getLogger(__name__)


_MY_DOMAIN_RE: Final = re.compile(
    r"^https://[a-z0-9.-]+\.(salesforce\.com|lightning\.force\.com)/?$"
)
_API_VERSION_RE: Final = re.compile(r"^v\d{2,3}\.\d$")
_ALIAS_FORBIDDEN: Final = "::"


_INSTRUCTIONS: Final[tuple[str, ...]] = (
    "In Setup, go to Apps → External Client Apps → External Client App Manager, "
    "then click New External Client App.",
    "External Client App Name: anything (e.g. “Salesforce Object Flow”).",
    "Contact Email: your address.",
    "Enable OAuth Settings.",
    f"Callback URL — paste exactly: {CALLBACK_URL}",
    "Selected OAuth Scopes:\n"
    "  • Manage user data via APIs (api)\n"
    "  • Perform requests on your behalf at any time "
    "(refresh_token, offline_access)",
    "Enable “Require Proof Key for Code Exchange (PKCE) Extension for "
    "Supported Authorization Flows”.",
    "Disable “Require Secret for Web Server Flow”.",
    "Disable “Require Secret for Refresh Token Flow” (it is enabled by default — turn it off).",
    "Save. Wait 2–10 minutes for activation.",
    "Open the app's Settings → Consumer Key and Secret. Copy the Consumer Key "
    "— that is your Client ID. The secret is not used by this app (PKCE).",
)


class ConnectionsPage:
    """The Connections page object expected by ``MainWindow._add_page``."""

    TITLE: ClassVar[str] = "Connections"

    def __init__(
        self,
        window: MainWindow,
        service: ConnectionsService,
        on_orgs_changed: Callable[[], None],
        on_active_org_changed: Callable[[], None] = lambda: None,
    ) -> None:
        self._window = window
        self._service = service
        self._on_orgs_changed = on_orgs_changed
        self._on_active_org_changed = on_active_org_changed
        self._orgs_group: Adw.PreferencesGroup | None = None

    # ----------------------------------------------------------- Page build
    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView:
        actual_header = header or Adw.HeaderBar()
        add_button = Gtk.Button(label="Add connection", icon_name="list-add-symbolic")
        add_button.add_css_class("suggested-action")
        add_button.set_tooltip_text("Add a new Salesforce connection")

        def _on_add_clicked(_button: Gtk.Button) -> None:
            self._open_add_dialog()

        add_button.connect("clicked", _on_add_clicked)
        actual_header.pack_end(add_button)

        toolbar_view, _page_box, content_box, _scrolled = make_page_layout(actual_header)

        content_box.append(self._build_instructions_group())
        self._orgs_group = self._build_orgs_group()
        content_box.append(self._orgs_group)

        return toolbar_view

    def _build_instructions_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Set up an External Client App")
        group.set_description(
            "Salesforce Object Flow uses your own External Client App so tokens stay "
            "scoped to your connection. Follow these one-time steps in Salesforce Setup."
        )

        expander = Gtk.Expander(label="Show step-by-step instructions")
        expander.set_margin_top(6)
        expander.set_margin_bottom(6)
        expander.set_margin_start(12)
        expander.set_margin_end(12)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(8)
        body.set_margin_start(12)

        for i, step in enumerate(_INSTRUCTIONS, start=1):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            number = Gtk.Label(label=f"{i:>2}.", xalign=0)
            number.add_css_class("dim-label")
            number.set_valign(Gtk.Align.START)
            row.append(number)
            text = Gtk.Label(label=step, xalign=0, wrap=True, hexpand=True)
            text.set_selectable(True)
            row.append(text)
            body.append(row)

        copy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        copy_row.set_margin_top(8)
        callback_label = Gtk.Label(label=CALLBACK_URL, xalign=0, hexpand=True)
        callback_label.add_css_class("monospace")
        callback_label.set_selectable(True)
        copy_row.append(callback_label)
        copy_button = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_button.set_tooltip_text("Copy callback URL")
        copy_button.add_css_class("flat")
        copy_button.connect("clicked", self._on_copy_callback)
        copy_row.append(copy_button)
        body.append(copy_row)

        expander.set_child(body)
        group.add(expander)
        return group

    def _build_orgs_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Connections")
        self._populate_orgs_group(group)
        return group

    def _populate_orgs_group(self, group: Adw.PreferencesGroup) -> None:
        # Adw.PreferencesGroup doesn't expose a "remove all rows" API; the
        # cleanest path is to iterate the internal listbox via a known
        # widget helper, but a stable approach is to set a fresh list
        # each time we refresh — easier: hold our own children and detach.
        #
        # Simpler: we let the caller construct a brand-new group and swap
        # it in. See ``_refresh_org_list``.
        orgs = self._service.list_orgs()
        active = self._window.config.active_org_alias
        if not orgs:
            group.add(self._build_empty_state())
            return
        for entry in orgs:
            group.add(self._build_org_row(entry, is_active=entry.alias == active))

    def _build_empty_state(self) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title("No connections yet")
        row.set_subtitle("Click “Add connection” above to add your first Salesforce connection.")
        return row

    def _build_org_row(self, entry: OrgEntry, *, is_active: bool) -> Adw.ActionRow:
        row = Adw.ActionRow()
        title = f"▶  {entry.alias}" if is_active else entry.alias
        row.set_title(title)
        sandbox_label = "Sandbox" if entry.is_sandbox else "Production"
        row.set_subtitle(f"{entry.instance_url} — {sandbox_label} · {entry.api_version}")
        if is_active:
            row.add_css_class("option-managed")

        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        suffix.set_valign(Gtk.Align.CENTER)

        alias = entry.alias

        def _on_test_clicked(_button: Gtk.Button) -> None:
            self._on_test(alias)

        def _on_reauth_clicked(_button: Gtk.Button) -> None:
            self._on_reauth(alias)

        test_btn = Gtk.Button(label="Test")
        test_btn.add_css_class("flat")
        test_btn.set_tooltip_text("Verify the connection by calling /limits")
        test_btn.connect("clicked", _on_test_clicked)
        suffix.append(test_btn)

        reauth_btn = Gtk.Button(label="Re-auth")
        reauth_btn.add_css_class("flat")
        reauth_btn.set_tooltip_text("Run the OAuth flow again with prompt=login")
        reauth_btn.connect("clicked", _on_reauth_clicked)
        suffix.append(reauth_btn)

        menu = Gio.Menu()
        if not is_active:
            menu.append("Activate", f"win.activate-org::{entry.alias}")
        menu.append("Remove…", f"win.remove-org::{entry.alias}")

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("view-more-symbolic")
        menu_btn.add_css_class("flat")
        menu_btn.set_menu_model(menu)
        suffix.append(menu_btn)

        row.add_suffix(suffix)
        return row

    # ----------------------------------------------------------- Public API
    def refresh_org_list(self) -> None:
        """Rebuild the “Connections” group in place after a config change."""
        if self._orgs_group is None:
            return
        new_group = Adw.PreferencesGroup()
        new_group.set_title("Connections")
        self._populate_orgs_group(new_group)

        parent = self._orgs_group.get_parent()
        if isinstance(parent, Gtk.Box):
            position = self._index_in_box(parent, self._orgs_group)
            parent.remove(self._orgs_group)
            if position is None:
                parent.append(new_group)
            else:
                self._insert_in_box(parent, new_group, position)
        self._orgs_group = new_group

    @staticmethod
    def _index_in_box(box: Gtk.Box, target: Gtk.Widget) -> int | None:
        index = 0
        child = box.get_first_child()
        while child is not None:
            if child is target:
                return index
            child = child.get_next_sibling()
            index += 1
        return None

    @staticmethod
    def _insert_in_box(box: Gtk.Box, widget: Gtk.Widget, index: int) -> None:
        # Gtk.Box has no insert-at-index API in GTK4; use ``insert_child_after``.
        if index == 0:
            box.prepend(widget)
            return
        sibling = box.get_first_child()
        for _ in range(index - 1):
            if sibling is None:
                break
            sibling = sibling.get_next_sibling()
        box.insert_child_after(widget, sibling)

    # ---------------------------------------------------------------- Add
    def _open_add_dialog(self) -> None:
        existing = {entry.alias for entry in self._service.list_orgs()}
        AddOrgDialog.present_singleton(
            self._window,
            existing_aliases=existing,
            on_submit=self._start_oauth,
        )

    def _start_oauth(self, request: AddOrgRequest) -> None:
        progress = OAuthProgressDialog(alias=request.alias)
        progress.present(self._window)

        cancelled = threading.Event()
        progress.connect_cancel(lambda: cancelled.set())

        def on_progress(event: ProgressEvent) -> None:
            GLib.idle_add(progress.update_progress, event)

        def worker() -> None:
            try:
                entry = self._service.add_org(request, on_progress, cancelled)
                GLib.idle_add(self._on_oauth_success, entry, progress)
            except ConnectionsError as exc:
                GLib.idle_add(self._on_oauth_error, str(exc), progress)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Unexpected OAuth failure")
                GLib.idle_add(self._on_oauth_error, f"Unexpected error: {exc}", progress)

        threading.Thread(target=worker, daemon=True, name=f"oauth-add-{request.alias}").start()

    def _on_oauth_success(self, entry: OrgEntry, progress: OAuthProgressDialog) -> bool:
        progress.dismiss()
        self._window.show_toast(f"Connected as “{entry.alias}”.")
        self.refresh_org_list()
        self._on_orgs_changed()
        # First org auto-becomes the active one in ConnectionsService.add_org;
        # downstream pages need to re-render against the new active alias.
        self._on_active_org_changed()
        return False

    def _on_oauth_error(self, message: str, progress: OAuthProgressDialog) -> bool:
        progress.dismiss()
        self._window.show_toast(message, timeout=6)
        return False

    # ---------------------------------------------------------- Re-auth
    def _on_reauth(self, alias: str) -> None:
        progress = OAuthProgressDialog(alias=alias, heading=f"Re-authenticating {alias}")
        progress.present(self._window)
        cancelled = threading.Event()
        progress.connect_cancel(lambda: cancelled.set())

        def on_progress(event: ProgressEvent) -> None:
            GLib.idle_add(progress.update_progress, event)

        def worker() -> None:
            try:
                self._service.reauth(alias, on_progress, cancelled)
                GLib.idle_add(self._on_reauth_success, alias, progress)
            except ConnectionsError as exc:
                GLib.idle_add(self._on_oauth_error, str(exc), progress)
            except Exception as exc:  # pragma: no cover
                log.exception("Unexpected re-auth failure")
                GLib.idle_add(self._on_oauth_error, f"Unexpected error: {exc}", progress)

        threading.Thread(target=worker, daemon=True, name=f"oauth-reauth-{alias}").start()

    def _on_reauth_success(self, alias: str, progress: OAuthProgressDialog) -> bool:
        progress.dismiss()
        self._window.show_toast(f"Re-authenticated {alias}.")
        self.refresh_org_list()
        self._on_orgs_changed()
        # Re-auth may have rotated the access token; let listeners react.
        self._on_active_org_changed()
        return False

    # ----------------------------------------------------------- Test
    def _on_test(self, alias: str) -> None:
        def worker() -> None:
            try:
                limits = self._service.test_connection(alias)
                GLib.idle_add(self._on_test_success, alias, limits)
            except Exception as exc:
                GLib.idle_add(self._on_test_error, alias, str(exc))

        threading.Thread(target=worker, daemon=True, name=f"oauth-test-{alias}").start()

    def _on_test_success(self, alias: str, _limits: dict[str, object]) -> bool:
        self._window.show_toast(f"{alias}: connection OK.")
        return False

    def _on_test_error(self, alias: str, message: str) -> bool:
        self._window.show_toast(f"{alias}: {message}", timeout=6)
        return False

    # ----------------------------------------------------------- Remove
    def request_remove(self, alias: str) -> None:
        """Public entry called from the window-scoped ``remove-org`` action."""
        confirm(
            self._window,
            heading=f"Remove {alias}?",
            body=(
                "Salesforce Object Flow will revoke the access token and delete the "
                "stored credentials for this connection. You can add it again later."
            ),
            label="Remove",
            on_confirm=lambda: self._do_remove(alias),
        )

    def _do_remove(self, alias: str) -> None:
        def worker() -> None:
            try:
                self._service.revoke(alias)
                GLib.idle_add(self._on_remove_success, alias)
            except Exception as exc:
                GLib.idle_add(self._on_remove_error, alias, str(exc))

        threading.Thread(target=worker, daemon=True, name=f"oauth-remove-{alias}").start()

    def _on_remove_success(self, alias: str) -> bool:
        self._window.show_toast(f"{alias} removed.")
        self.refresh_org_list()
        self._on_orgs_changed()
        # If the removed org was active, downstream pages need to re-render
        # their empty-states.
        self._on_active_org_changed()
        return False

    def _on_remove_error(self, alias: str, message: str) -> bool:
        self._window.show_toast(f"Could not remove {alias} — {message}", timeout=6)
        # Refresh anyway in case the local cleanup partially succeeded.
        self.refresh_org_list()
        self._on_orgs_changed()
        self._on_active_org_changed()
        return False

    # ------------------------------------------------------------ Helpers
    def _on_copy_callback(self, _button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.set(CALLBACK_URL)
        self._window.show_toast("Callback URL copied.")


# ====================================================================
# Add Org dialog
# ====================================================================


class AddOrgDialog(Adw.AlertDialog):
    """Form dialog collecting the four fields needed to start the PKCE flow."""

    _opened_dialogs: dict[type, Adw.Dialog] = {}

    def __init__(
        self,
        *,
        existing_aliases: set[str],
        on_submit: Callable[[AddOrgRequest], None],
    ) -> None:
        super().__init__()
        self.set_heading("Add Salesforce connection")
        self.set_body(
            "Add a Salesforce connection by pointing this app at your External Client App."
        )
        self._existing_aliases = existing_aliases
        self._on_submit = on_submit

        self._alias_row = Adw.EntryRow()
        self._alias_row.set_title("Alias")

        self._domain_row = Adw.EntryRow()
        self._domain_row.set_title("My Domain URL")

        self._client_id_row = Adw.EntryRow()
        self._client_id_row.set_title("Client ID")

        self._sandbox_row = Adw.SwitchRow()
        self._sandbox_row.set_title("Sandbox")
        self._sandbox_row.set_subtitle(
            "Tag this connection as a sandbox in the UI (does not affect routing)."
        )

        self._api_row = Adw.EntryRow()
        self._api_row.set_title("API Version")
        self._api_row.set_text(DEFAULT_API_VERSION)

        group = Adw.PreferencesGroup()
        group.add(self._alias_row)
        group.add(self._domain_row)
        group.add(self._client_id_row)
        group.add(self._sandbox_row)
        group.add(self._api_row)
        self.set_extra_child(group)

        self.add_response("cancel", "Cancel")
        self.add_response("connect", "Connect")
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("connect")
        self.set_close_response("cancel")
        self.set_response_enabled("connect", False)

        self._alias_row.connect("notify::text", self._validate)
        self._domain_row.connect("notify::text", self._validate)
        self._client_id_row.connect("notify::text", self._validate)
        self._api_row.connect("notify::text", self._validate)

        self.connect("response", self._on_response)

    @classmethod
    def present_singleton(
        cls,
        parent: Gtk.Widget,
        *,
        existing_aliases: set[str],
        on_submit: Callable[[AddOrgRequest], None],
    ) -> None:
        if cls in AddOrgDialog._opened_dialogs:
            return
        dialog = cls(existing_aliases=existing_aliases, on_submit=on_submit)
        AddOrgDialog._opened_dialogs[cls] = dialog
        dialog.connect("closed", cls._on_singleton_closed)
        dialog.present(parent)

    @classmethod
    def _on_singleton_closed(cls, _dialog: Adw.Dialog) -> None:
        AddOrgDialog._opened_dialogs.pop(cls, None)

    # ----------------------------------------------------------- Validation
    def _validate(self, *_args: object) -> None:
        alias_ok = self._validate_alias(set_error=True)
        domain_ok = self._validate_domain(set_error=True)
        client_id_ok = self._validate_client_id(set_error=True)
        api_ok = self._validate_api(set_error=True)
        self.set_response_enabled("connect", alias_ok and domain_ok and client_id_ok and api_ok)

    def _validate_alias(self, *, set_error: bool) -> bool:
        alias = self._alias_row.get_text().strip()
        ok = bool(alias) and _ALIAS_FORBIDDEN not in alias and alias not in self._existing_aliases
        if set_error:
            _set_field_error(self._alias_row, not ok and bool(alias))
        return ok

    def _validate_domain(self, *, set_error: bool) -> bool:
        url = self._domain_row.get_text().strip()
        ok = bool(_MY_DOMAIN_RE.match(url))
        if set_error:
            _set_field_error(self._domain_row, not ok and bool(url))
        return ok

    def _validate_client_id(self, *, set_error: bool) -> bool:
        client_id = self._client_id_row.get_text().strip()
        ok = bool(client_id) and " " not in client_id
        if set_error:
            _set_field_error(self._client_id_row, not ok and bool(client_id))
        return ok

    def _validate_api(self, *, set_error: bool) -> bool:
        version = self._api_row.get_text().strip()
        ok = bool(_API_VERSION_RE.match(version))
        if set_error:
            _set_field_error(self._api_row, not ok and bool(version))
        return ok

    # ----------------------------------------------------------- Submit
    def _on_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "connect":
            return
        request = AddOrgRequest(
            alias=self._alias_row.get_text().strip(),
            my_domain_url=self._domain_row.get_text().strip().rstrip("/"),
            client_id=self._client_id_row.get_text().strip(),
            is_sandbox=self._sandbox_row.get_active(),
            api_version=self._api_row.get_text().strip(),
        )
        self._on_submit(request)


def _set_field_error(row: Adw.EntryRow, has_error: bool) -> None:
    if has_error:
        row.add_css_class("field-error")
    else:
        row.remove_css_class("field-error")


# ====================================================================
# OAuth progress dialog
# ====================================================================


class OAuthProgressDialog(Adw.Dialog):
    """Spinner + Cancel dialog shown while the PKCE handshake runs."""

    def __init__(self, *, alias: str, heading: str | None = None) -> None:
        super().__init__()
        self.set_title(heading or f"Connecting to {alias}")
        self.set_can_close(False)
        self.set_content_width(420)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(20)
        outer.set_margin_bottom(20)
        outer.set_margin_start(24)
        outer.set_margin_end(24)

        header_bar = Adw.HeaderBar()
        header_bar.set_show_start_title_buttons(False)
        header_bar.set_show_end_title_buttons(False)
        header_bar.set_title_widget(Adw.WindowTitle(title=heading or f"Connecting to {alias}"))

        spinner_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(24, 24)
        self._spinner.start()
        spinner_row.append(self._spinner)
        self._status_label = Gtk.Label(
            label="Waiting for browser…",
            xalign=0,
            wrap=True,
            hexpand=True,
        )
        spinner_row.append(self._status_label)
        outer.append(spinner_row)

        body_label = Gtk.Label(
            label=(
                "Complete the authorization in your browser. This dialog will close automatically."
            ),
            xalign=0,
            wrap=True,
        )
        body_label.add_css_class("dim-label")
        outer.append(body_label)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.set_halign(Gtk.Align.END)
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        outer.append(self._cancel_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header_bar)
        toolbar.set_content(outer)
        self.set_child(toolbar)

        self._cancel_callback: Callable[[], None] | None = None
        self._dismissed = False

    def connect_cancel(self, callback: Callable[[], None]) -> None:
        self._cancel_callback = callback

    def update_progress(self, event: ProgressEvent) -> bool:
        if event == "waiting_for_browser":
            self._status_label.set_label("Waiting for browser…")
        elif event == "exchanging_code":
            self._status_label.set_label("Exchanging authorization code…")
            self._cancel_btn.set_sensitive(False)
        elif event == "persisting":
            self._status_label.set_label("Saving credentials…")
        elif event == "done":
            self._status_label.set_label("Done.")
        return False

    def dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self._spinner.stop()
        self.set_can_close(True)
        self.close()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self._cancel_btn.set_sensitive(False)
        self._status_label.set_label("Cancelling…")
        if self._cancel_callback is not None:
            self._cancel_callback()
