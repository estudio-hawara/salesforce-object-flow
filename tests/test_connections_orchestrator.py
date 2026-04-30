"""Tests for ``services/connections.py``.

The orchestrator is exercised end-to-end with three injected seams:

- a ``loopback_factory`` that returns a fake server immediately yielding a
  known ``(code, state)``;
- an ``httpx.MockTransport`` for Salesforce's token endpoints;
- a fake keyring patched onto ``salesforce_object_flow.core.credentials``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

import httpx
import pytest

from salesforce_object_flow.core import credentials as credentials_module
from salesforce_object_flow.core.config import DEFAULT_API_VERSION, Config, OrgEntry
from salesforce_object_flow.services.connections import (
    AddOrgRequest,
    ConnectionsError,
    ConnectionsService,
    ProgressEvent,
)
from salesforce_object_flow.services.loopback import CallbackResult, LoopbackError


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if (service, key) not in self.store:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("not found")
        del self.store[(service, key)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials_module, "keyring", fake)
    return fake


class FakeLoopback:
    """Returns a pre-canned callback the moment ``wait()`` is called."""

    def __init__(
        self,
        result: CallbackResult | LoopbackError,
        *,
        start_error: LoopbackError | None = None,
    ) -> None:
        self._result = result
        self._start_error = start_error
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True
        if self._start_error is not None:
            raise self._start_error

    def wait(self, timeout: float) -> CallbackResult:
        if isinstance(self._result, LoopbackError):
            raise self._result
        return self._result

    def stop(self) -> None:
        self.stopped = True


def _make_service(
    *,
    config: Config | None = None,
    loopback: FakeLoopback | None = None,
    transport_handler: Any = None,
    browser_calls: list[str] | None = None,
) -> tuple[ConnectionsService, Config, list[str]]:
    config = config if config is not None else Config()
    loopback = loopback or FakeLoopback(CallbackResult(code="AUTH_CODE", state="STATE"))
    browser_calls = browser_calls if browser_calls is not None else []

    saved: list[Config] = []

    def config_save() -> None:
        saved.append(Config(**{k: v for k, v in asdict(config).items() if k != "orgs"}))

    def client_factory() -> httpx.Client:
        if transport_handler is None:
            raise AssertionError("test forgot to provide transport_handler")
        return httpx.Client(transport=httpx.MockTransport(transport_handler))

    def fake_browser(url: str) -> bool:
        browser_calls.append(url)
        return True

    def loopback_factory(expected_state: str) -> FakeLoopback:
        # Patch the loopback's expected state on construction so its echoed
        # value matches the test's canned CallbackResult.state.
        if isinstance(loopback._result, CallbackResult):  # noqa: SLF001
            loopback._result = CallbackResult(  # noqa: SLF001
                code=loopback._result.code,
                state=expected_state,  # noqa: SLF001
            )
        return loopback

    service = ConnectionsService(
        config=config,
        config_save=config_save,
        client_factory=client_factory,
        browser_open=fake_browser,
        loopback_factory=loopback_factory,
    )
    return service, config, browser_calls


def _token_handler(
    *,
    access_token: str = "AT",
    refresh_token: str | None = "RT",
    instance_url: str = "https://acme.my.salesforce.com",
    status: int = 200,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(
                status,
                json={
                    "error": "invalid_grant",
                    "error_description": "code expired",
                },
            )
        body: dict[str, Any] = {
            "access_token": access_token,
            "instance_url": instance_url,
        }
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
        return httpx.Response(status, json=body)

    return handler


def _request_factory(alias: str = "prod") -> AddOrgRequest:
    return AddOrgRequest(
        alias=alias,
        my_domain_url="https://acme.my.salesforce.com",
        client_id="cid",
        is_sandbox=False,
        api_version=DEFAULT_API_VERSION,
    )


def _drain(events: list[ProgressEvent]) -> Iterator[ProgressEvent]:
    yield from events


def test_add_org_persists_config_and_keyring(fake_keyring: FakeKeyring) -> None:
    events: list[ProgressEvent] = []
    service, config, browser_calls = _make_service(transport_handler=_token_handler())

    entry = service.add_org(_request_factory(), events.append, threading.Event())

    assert entry.alias == "prod"
    assert entry.instance_url == "https://acme.my.salesforce.com"
    assert entry.api_version == DEFAULT_API_VERSION
    assert config.find_org("prod") is not None
    assert config.active_org_alias == "prod"

    creds = credentials_module.get("prod")
    assert creds is not None
    assert creds.access_token == "AT"
    assert creds.refresh_token == "RT"

    assert events == [
        "waiting_for_browser",
        "exchanging_code",
        "persisting",
        "done",
    ]
    assert browser_calls and "code_challenge" in browser_calls[0]


def test_add_org_duplicate_alias_raises(fake_keyring: FakeKeyring) -> None:
    config = Config(orgs=[OrgEntry("prod", "u", "u", "cid")])
    service, _, _ = _make_service(config=config, transport_handler=_token_handler())

    with pytest.raises(ConnectionsError, match="already exists"):
        service.add_org(_request_factory("prod"), lambda _: None, threading.Event())


def test_add_org_does_not_persist_on_token_exchange_failure(
    fake_keyring: FakeKeyring,
) -> None:
    service, config, _ = _make_service(transport_handler=_token_handler(status=400))

    with pytest.raises(ConnectionsError):
        service.add_org(_request_factory(), lambda _: None, threading.Event())

    assert config.orgs == []
    assert credentials_module.get("prod") is None


def test_add_org_propagates_loopback_start_error(fake_keyring: FakeKeyring) -> None:
    loopback = FakeLoopback(
        CallbackResult(code="C", state="S"),
        start_error=LoopbackError("Port 8765 is already in use."),
    )
    service, config, _ = _make_service(loopback=loopback, transport_handler=_token_handler())

    with pytest.raises(ConnectionsError, match="8765"):
        service.add_org(_request_factory(), lambda _: None, threading.Event())

    assert config.orgs == []
    assert loopback.stopped is True


def test_add_org_propagates_state_mismatch(fake_keyring: FakeKeyring) -> None:
    loopback = FakeLoopback(LoopbackError("Authorization state mismatch — stale tab."))
    service, config, _ = _make_service(loopback=loopback, transport_handler=_token_handler())

    with pytest.raises(ConnectionsError, match="state mismatch"):
        service.add_org(_request_factory(), lambda _: None, threading.Event())

    assert config.orgs == []


def test_add_org_cancellation(fake_keyring: FakeKeyring) -> None:
    class BlockingLoopback:
        started = False
        stopped = False

        def start(self) -> None:
            self.started = True

        def wait(self, timeout: float) -> CallbackResult:
            raise LoopbackError("Authorization timed out before the callback arrived.")

        def stop(self) -> None:
            self.stopped = True

    blocking = BlockingLoopback()

    def loopback_factory(_state: str) -> BlockingLoopback:
        return blocking

    config = Config()
    service = ConnectionsService(
        config=config,
        config_save=lambda: None,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_token_handler())),
        browser_open=lambda _: True,
        loopback_factory=loopback_factory,
    )

    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ConnectionsError, match="Cancelled"):
        service.add_org(_request_factory(), lambda _: None, cancelled)

    assert blocking.stopped is True


def test_refresh_rotates_refresh_token(fake_keyring: FakeKeyring) -> None:
    config = Config(
        orgs=[
            OrgEntry(
                "prod", "https://acme.my.salesforce.com", "https://acme.my.salesforce.com", "cid"
            )
        ]
    )
    service, _, _ = _make_service(
        config=config, transport_handler=_token_handler(access_token="AT2", refresh_token="RT2")
    )
    credentials_module.set(
        "prod",
        credentials_module.OrgCredentials(
            instance_url="https://acme.my.salesforce.com",
            access_token="OLD_AT",
            refresh_token="OLD_RT",
        ),
    )

    new_creds = service.refresh("prod")

    assert new_creds.access_token == "AT2"
    assert new_creds.refresh_token == "RT2"
    stored = credentials_module.get("prod")
    assert stored is not None
    assert stored.refresh_token == "RT2"


def test_refresh_keeps_old_refresh_token_when_response_omits_it(
    fake_keyring: FakeKeyring,
) -> None:
    config = Config(
        orgs=[OrgEntry("prod", "https://x.my.salesforce.com", "https://x.my.salesforce.com", "cid")]
    )
    service, _, _ = _make_service(
        config=config,
        transport_handler=_token_handler(access_token="AT3", refresh_token=None),
    )
    credentials_module.set(
        "prod",
        credentials_module.OrgCredentials(
            instance_url="https://x.my.salesforce.com",
            access_token="OLD_AT",
            refresh_token="STAY_RT",
        ),
    )

    new_creds = service.refresh("prod")

    assert new_creds.access_token == "AT3"
    assert new_creds.refresh_token == "STAY_RT"


def test_remove_clears_active_org_alias(fake_keyring: FakeKeyring) -> None:
    config = Config(
        active_org_alias="prod",
        orgs=[OrgEntry("prod", "u", "u", "cid")],
    )
    service, _, _ = _make_service(config=config, transport_handler=_token_handler())
    credentials_module.set(
        "prod",
        credentials_module.OrgCredentials(instance_url="u", access_token="AT", refresh_token=None),
    )

    service.remove("prod")

    assert config.orgs == []
    assert config.active_org_alias is None
    assert credentials_module.get("prod") is None


def test_test_connection_returns_limits(fake_keyring: FakeKeyring) -> None:
    entry = OrgEntry(
        "prod", "https://acme.my.salesforce.com", "https://acme.my.salesforce.com", "cid"
    )
    config = Config(orgs=[entry])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/limits"):
            return httpx.Response(
                200, json={"DailyApiRequests": {"Max": 15000, "Remaining": 14800}}
            )
        return httpx.Response(404)

    service, _, _ = _make_service(config=config, transport_handler=handler)
    credentials_module.set(
        "prod",
        credentials_module.OrgCredentials(
            instance_url="https://acme.my.salesforce.com",
            access_token="AT",
            refresh_token="RT",
        ),
    )

    limits = service.test_connection("prod")

    assert limits["DailyApiRequests"]["Max"] == 15000


def test_test_connection_retries_once_on_401(fake_keyring: FakeKeyring) -> None:
    entry = OrgEntry(
        "prod", "https://acme.my.salesforce.com", "https://acme.my.salesforce.com", "cid"
    )
    config = Config(orgs=[entry])

    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/limits"):
            call_log.append(request.headers["Authorization"])
            if len(call_log) == 1:
                return httpx.Response(
                    401, json={"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}
                )
            return httpx.Response(
                200, json={"DailyApiRequests": {"Max": 15000, "Remaining": 14800}}
            )
        if path.endswith("/services/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "AT_NEW",
                    "instance_url": "https://acme.my.salesforce.com",
                },
            )
        return httpx.Response(404)

    service, _, _ = _make_service(config=config, transport_handler=handler)
    credentials_module.set(
        "prod",
        credentials_module.OrgCredentials(
            instance_url="https://acme.my.salesforce.com",
            access_token="AT_OLD",
            refresh_token="RT",
        ),
    )

    limits = service.test_connection("prod")

    assert limits["DailyApiRequests"]["Remaining"] == 14800
    assert call_log == ["Bearer AT_OLD", "Bearer AT_NEW"]
    stored = credentials_module.get("prod")
    assert stored is not None
    assert stored.access_token == "AT_NEW"


def test_set_active_unknown_alias_raises(fake_keyring: FakeKeyring) -> None:
    service, _, _ = _make_service(transport_handler=_token_handler())

    with pytest.raises(ConnectionsError):
        service.set_active("nope")


def test_set_active_clears(fake_keyring: FakeKeyring) -> None:
    config = Config(
        active_org_alias="prod",
        orgs=[OrgEntry("prod", "u", "u", "cid")],
    )
    service, _, _ = _make_service(config=config, transport_handler=_token_handler())

    service.set_active(None)

    assert config.active_org_alias is None
