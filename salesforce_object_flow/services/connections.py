"""Connections orchestrator.

The single entry point the UI uses for the OAuth handshake and per-org
operations. Sync; intended to run on a worker thread (the page wraps every
callback in ``GLib.idle_add`` to land back on the main loop).

The orchestrator owns no GTK imports and no global state — it is given a
``Config`` and a ``config_save`` callback by the caller so persistence stays
explicit and testable.
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from salesforce_object_flow.core import credentials
from salesforce_object_flow.core.config import DEFAULT_API_VERSION, Config, OrgEntry
from salesforce_object_flow.core.credentials import (
    CredentialsError,
    OrgCredentials,
)
from salesforce_object_flow.services.api import SalesforceClient, update_creds_after_refresh
from salesforce_object_flow.services.loopback import (
    CallbackResult,
    LoopbackError,
    LoopbackServer,
)
from salesforce_object_flow.services.oauth import (
    OAuthError,
    TokenBundle,
    build_authorize_url,
    exchange_code,
    generate_pkce,
    refresh_access_token,
    revoke,
)

log = logging.getLogger(__name__)


ProgressEvent = Literal[
    "waiting_for_browser",
    "exchanging_code",
    "persisting",
    "done",
]
ProgressCallback = Callable[[ProgressEvent], None]


class ConnectionsError(RuntimeError):
    """Top-level orchestrator failure. Message is safe to toast verbatim."""


@dataclass(frozen=True, slots=True)
class AddOrgRequest:
    alias: str
    my_domain_url: str
    client_id: str
    is_sandbox: bool
    api_version: str = DEFAULT_API_VERSION


class _LoopbackProto(Protocol):
    def start(self) -> None: ...
    def wait(self, timeout: float) -> CallbackResult: ...
    def stop(self) -> None: ...


LoopbackFactory = Callable[[str], _LoopbackProto]


def _default_client_factory() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0),
    )


def _default_loopback_factory(expected_state: str) -> _LoopbackProto:
    return LoopbackServer(expected_state=expected_state)


class ConnectionsService:
    """High-level operations on the user's connected orgs."""

    AUTHORIZE_TIMEOUT_SECONDS: float = 300.0

    def __init__(
        self,
        config: Config,
        config_save: Callable[[], None],
        *,
        client_factory: Callable[[], httpx.Client] = _default_client_factory,
        browser_open: Callable[[str], bool] = webbrowser.open,
        loopback_factory: LoopbackFactory = _default_loopback_factory,
    ) -> None:
        self._config = config
        self._config_save = config_save
        self._client_factory = client_factory
        self._browser_open = browser_open
        self._loopback_factory = loopback_factory

    # ------------------------------------------------------------------ Add
    def add_org(
        self,
        request: AddOrgRequest,
        progress: ProgressCallback,
        cancelled: threading.Event,
    ) -> OrgEntry:
        """Run the full PKCE handshake and persist the resulting org.

        Sync; designed to be invoked from a worker thread. Cooperative
        cancellation: the caller flips ``cancelled`` and the orchestrator
        returns at the next polling boundary.

        Persistence is all-or-nothing: if the keyring write fails, the config
        is not modified.
        """
        if self._config.find_org(request.alias) is not None:
            raise ConnectionsError(f"A connection with alias '{request.alias}' already exists.")

        challenge = generate_pkce()
        server = self._loopback_factory(challenge.state)

        try:
            try:
                server.start()
            except LoopbackError as exc:
                raise ConnectionsError(str(exc)) from exc

            authorize_url = build_authorize_url(request.my_domain_url, request.client_id, challenge)
            self._browser_open(authorize_url)
            progress("waiting_for_browser")

            result = self._await_callback(server, cancelled)
            progress("exchanging_code")

            with self._client_factory() as client:
                try:
                    bundle = exchange_code(
                        client,
                        request.my_domain_url,
                        request.client_id,
                        code=result.code,
                        verifier=challenge.verifier,
                    )
                except OAuthError as exc:
                    raise ConnectionsError(str(exc)) from exc
                except httpx.RequestError as exc:
                    raise ConnectionsError(f"Could not exchange authorization code: {exc}") from exc

            progress("persisting")
            entry = self._persist_new_org(request, bundle)
            progress("done")
            return entry
        finally:
            server.stop()

    def _await_callback(self, server: _LoopbackProto, cancelled: threading.Event) -> CallbackResult:
        deadline = time.monotonic() + self.AUTHORIZE_TIMEOUT_SECONDS
        while True:
            if cancelled.is_set():
                raise ConnectionsError("Cancelled.")
            if time.monotonic() >= deadline:
                raise ConnectionsError(
                    "Authorization timed out. Did you complete the login in your browser?"
                )
            try:
                return server.wait(timeout=1.0)
            except LoopbackError as exc:
                if "timed out" in str(exc).lower():
                    continue
                raise ConnectionsError(str(exc)) from exc

    def _persist_new_org(self, request: AddOrgRequest, bundle: TokenBundle) -> OrgEntry:
        # Write the keyring entry first; if that fails, the config remains
        # untouched and the user can retry without orphaned UI state.
        creds = OrgCredentials(
            instance_url=bundle.instance_url,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
        )
        try:
            credentials.set(request.alias, creds)
        except CredentialsError as exc:
            raise ConnectionsError(str(exc)) from exc

        entry = OrgEntry(
            alias=request.alias,
            instance_url=bundle.instance_url,
            my_domain_url=request.my_domain_url,
            client_id=request.client_id,
            is_sandbox=request.is_sandbox,
            api_version=request.api_version,
        )
        self._config.upsert_org(entry)
        if self._config.active_org_alias is None:
            self._config.active_org_alias = entry.alias
        self._config_save()
        return entry

    # ---------------------------------------------------------------- Reauth
    def reauth(
        self,
        alias: str,
        progress: ProgressCallback,
        cancelled: threading.Event,
    ) -> OrgEntry:
        """Re-run the PKCE handshake for an existing org with ``prompt=login``.

        Replaces the stored credentials in keyring; the ``OrgEntry`` itself is
        updated only if Salesforce returns a different ``instance_url``.
        """
        entry = self._require_entry(alias)
        challenge = generate_pkce()
        server = self._loopback_factory(challenge.state)

        try:
            try:
                server.start()
            except LoopbackError as exc:
                raise ConnectionsError(str(exc)) from exc

            authorize_url = build_authorize_url(
                entry.my_domain_url, entry.client_id, challenge, force_login=True
            )
            self._browser_open(authorize_url)
            progress("waiting_for_browser")

            result = self._await_callback(server, cancelled)
            progress("exchanging_code")

            with self._client_factory() as client:
                try:
                    bundle = exchange_code(
                        client,
                        entry.my_domain_url,
                        entry.client_id,
                        code=result.code,
                        verifier=challenge.verifier,
                    )
                except OAuthError as exc:
                    raise ConnectionsError(str(exc)) from exc
                except httpx.RequestError as exc:
                    raise ConnectionsError(f"Could not exchange authorization code: {exc}") from exc

            progress("persisting")
            creds = OrgCredentials(
                instance_url=bundle.instance_url,
                access_token=bundle.access_token,
                refresh_token=bundle.refresh_token,
            )
            try:
                credentials.set(alias, creds)
            except CredentialsError as exc:
                raise ConnectionsError(str(exc)) from exc

            if bundle.instance_url and bundle.instance_url != entry.instance_url:
                entry.instance_url = bundle.instance_url
                self._config_save()

            progress("done")
            return entry
        finally:
            server.stop()

    # -------------------------------------------------------------- Refresh
    def refresh(self, alias: str) -> OrgCredentials:
        entry = self._require_entry(alias)
        existing = credentials.get(alias)
        if existing is None or existing.refresh_token is None:
            raise ConnectionsError(
                f"No refresh token stored for '{alias}'. Re-authenticate to continue."
            )

        with self._client_factory() as client:
            try:
                bundle = refresh_access_token(
                    client, entry.my_domain_url, entry.client_id, existing.refresh_token
                )
            except OAuthError as exc:
                raise ConnectionsError(str(exc)) from exc
            except httpx.RequestError as exc:
                raise ConnectionsError(f"Could not refresh token: {exc}") from exc

        new_creds = update_creds_after_refresh(existing, bundle.access_token, bundle.refresh_token)
        # Salesforce sometimes returns a fresh instance_url; honour it.
        if bundle.instance_url and bundle.instance_url != new_creds.instance_url:
            new_creds = OrgCredentials(
                instance_url=bundle.instance_url,
                access_token=new_creds.access_token,
                refresh_token=new_creds.refresh_token,
            )
            entry.instance_url = bundle.instance_url
            self._config_save()

        credentials.set(alias, new_creds)
        return new_creds

    # ------------------------------------------------------------- Remove
    def revoke(self, alias: str) -> None:
        """Try to revoke the access token, then delete locally."""
        entry = self._require_entry(alias)
        existing = credentials.get(alias)
        if existing is not None:
            with self._client_factory() as client:
                revoke(client, entry.my_domain_url, existing.access_token)
        self._delete_local(alias)

    def remove(self, alias: str) -> None:
        """Local cleanup only (no revoke endpoint call)."""
        self._require_entry(alias)
        self._delete_local(alias)

    def _delete_local(self, alias: str) -> None:
        try:
            credentials.delete(alias)
        except CredentialsError as exc:
            log.warning("Failed to delete keyring entry for %s: %s", alias, exc)
        self._config.remove_org(alias)
        self._config_save()

    # ----------------------------------------------------------- Query / Test
    def list_orgs(self) -> list[OrgEntry]:
        return list(self._config.orgs)

    def set_active(self, alias: str | None) -> None:
        if alias is not None:
            self._require_entry(alias)
        self._config.active_org_alias = alias
        self._config_save()

    def test_connection(self, alias: str) -> dict[str, Any]:
        with self.get_authenticated_client(alias) as sf_client:
            return sf_client.limits()

    @contextmanager
    def get_authenticated_client(self, alias: str) -> Generator[SalesforceClient]:
        """Yield a ``SalesforceClient`` wired for *alias*.

        The yielded client owns the underlying ``httpx.Client`` only for the
        duration of the ``with`` block. The 401-refresh path is wired to
        :meth:`refresh` and persists rotated credentials to the keyring.
        """
        entry = self._require_entry(alias)
        creds = credentials.get(alias)
        if creds is None:
            raise ConnectionsError(
                f"No stored credentials for '{alias}'. Re-authenticate to continue."
            )

        with self._client_factory() as http_client:
            yield SalesforceClient(
                creds=creds,
                api_version=entry.api_version,
                client=http_client,
                refresh_fn=lambda: self.refresh(alias),
                on_token_refresh=lambda new: credentials.set(alias, new),
            )

    # ----------------------------------------------------------------- Util
    def _require_entry(self, alias: str) -> OrgEntry:
        entry = self._config.find_org(alias)
        if entry is None:
            raise ConnectionsError(f"No connection with alias '{alias}' is registered.")
        return entry
