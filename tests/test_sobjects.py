"""Tests for ``services/sobjects.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from salesforce_object_flow.core import credentials as credentials_module
from salesforce_object_flow.core.cache import JsonCache
from salesforce_object_flow.core.config import DEFAULT_API_VERSION, Config, OrgEntry
from salesforce_object_flow.core.credentials import OrgCredentials
from salesforce_object_flow.services.connections import ConnectionsError, ConnectionsService
from salesforce_object_flow.services.sobjects import SObjectService
from tests.conftest import FakeKeyring

INSTANCE = "https://acme.my.salesforce.com"
ALIAS = "prod"


def _entry(alias: str = ALIAS, *, instance_url: str = INSTANCE) -> OrgEntry:
    return OrgEntry(
        alias=alias,
        instance_url=instance_url,
        my_domain_url=instance_url,
        client_id="cid",
        api_version=DEFAULT_API_VERSION,
    )


def _seed_creds(alias: str = ALIAS, *, instance_url: str = INSTANCE) -> None:
    credentials_module.set(
        alias,
        OrgCredentials(instance_url=instance_url, access_token="AT", refresh_token="RT"),
    )


def _make_service(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: JsonCache,
    *,
    config: Config | None = None,
) -> SObjectService:
    config = config if config is not None else Config(orgs=[_entry()])

    def client_factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    connections = ConnectionsService(
        config=config,
        config_save=lambda: None,
        client_factory=client_factory,
    )
    return SObjectService(connections, cache)


def _list_response(*names: str) -> dict[str, Any]:
    return {
        "encoding": "UTF-8",
        "maxBatchSize": 200,
        "sobjects": [
            {
                "name": name,
                "label": name,
                "labelPlural": f"{name}s",
                "custom": name.endswith("__c"),
                "queryable": True,
                "createable": True,
                "updateable": True,
                "deletable": True,
                "keyPrefix": "001" if name == "Account" else None,
            }
            for name in names
        ],
    }


def _account_describe() -> dict[str, Any]:
    return {
        "name": "Account",
        "label": "Account",
        "custom": False,
        "fields": [
            {
                "name": "Id",
                "label": "Account ID",
                "type": "id",
                "length": 18,
                "nillable": False,
                "createable": False,
                "updateable": False,
                "unique": True,
                "externalId": False,
            },
            {
                "name": "Name",
                "label": "Account Name",
                "type": "string",
                "length": 255,
                "nillable": False,
                "createable": True,
                "updateable": True,
                "unique": False,
                "externalId": False,
            },
            {
                "name": "Industry",
                "label": "Industry",
                "type": "picklist",
                "length": 0,
                "nillable": True,
                "createable": True,
                "updateable": True,
                "unique": False,
                "externalId": False,
                "picklistValues": [
                    {"value": "Banking", "label": "Banking", "active": True, "defaultValue": False},
                    {"value": "Retail", "label": "Retail", "active": True, "defaultValue": True},
                ],
            },
            {
                "name": "OwnerId",
                "label": "Owner ID",
                "type": "reference",
                "length": 18,
                "nillable": False,
                "createable": True,
                "updateable": True,
                "unique": False,
                "externalId": False,
                "referenceTo": ["User", "Group"],
                "relationshipName": "Owner",
            },
        ],
    }


# ====================================================================
# list_sobjects
# ====================================================================


def test_list_sobjects_calls_endpoint_and_caches(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_list_response("Account", "Contact"))

    service = _make_service(handler, tmp_cache)

    first = service.list_sobjects(ALIAS)
    second = service.list_sobjects(ALIAS)

    assert [s.name for s in first] == ["Account", "Contact"]
    assert [s.name for s in second] == ["Account", "Contact"]
    assert calls == [f"/services/data/{DEFAULT_API_VERSION}/sobjects"]


def test_list_sobjects_returns_summaries(fake_keyring: FakeKeyring, tmp_cache: JsonCache) -> None:
    _seed_creds()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_list_response("Account", "Foo__c"))

    service = _make_service(handler, tmp_cache)
    summaries = service.list_sobjects(ALIAS)

    account = summaries[0]
    foo = summaries[1]
    assert account.name == "Account"
    assert account.custom is False
    assert account.key_prefix == "001"
    assert foo.custom is True
    assert foo.key_prefix is None


def test_list_sobjects_propagates_api_error(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errorCode": "SERVER_ERROR", "message": "boom"})

    service = _make_service(handler, tmp_cache)
    with pytest.raises(Exception, match="SERVER_ERROR|boom"):
        service.list_sobjects(ALIAS)
    # Cache must remain empty.
    assert tmp_cache.get(service._key_list(_entry())) is None  # noqa: SLF001


def test_refresh_list_invalidates_and_refetches(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_list_response("Account"))

    service = _make_service(handler, tmp_cache)
    service.list_sobjects(ALIAS)
    service.refresh_list(ALIAS)

    assert len(calls) == 2


def test_refresh_list_does_not_invalidate_describes(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "describe" in request.url.path:
            return httpx.Response(200, json=_account_describe())
        return httpx.Response(200, json=_list_response("Account"))

    service = _make_service(handler, tmp_cache)
    service.list_sobjects(ALIAS)
    service.describe(ALIAS, "Account")
    service.refresh_list(ALIAS)
    service.describe(ALIAS, "Account")  # cached; no new call

    describe_calls = [p for p in calls if "describe" in p]
    assert len(describe_calls) == 1


def test_list_sobjects_unknown_alias_raises(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_list_response("Account"))

    service = _make_service(handler, tmp_cache)
    with pytest.raises(ConnectionsError):
        service.list_sobjects("nope")


# ====================================================================
# describe
# ====================================================================


def test_describe_calls_endpoint_and_caches(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_account_describe())

    service = _make_service(handler, tmp_cache)
    first = service.describe(ALIAS, "Account")
    second = service.describe(ALIAS, "Account")

    assert first.name == "Account"
    assert second.name == "Account"
    assert calls == [f"/services/data/{DEFAULT_API_VERSION}/sobjects/Account/describe"]


def test_describe_parses_picklist_values(fake_keyring: FakeKeyring, tmp_cache: JsonCache) -> None:
    _seed_creds()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_account_describe())

    service = _make_service(handler, tmp_cache)
    describe = service.describe(ALIAS, "Account")
    industry = next(f for f in describe.fields if f.name == "Industry")
    assert industry.type == "picklist"
    assert [v.value for v in industry.picklist_values] == ["Banking", "Retail"]
    assert industry.picklist_values[1].default_value is True


def test_describe_parses_reference_field(fake_keyring: FakeKeyring, tmp_cache: JsonCache) -> None:
    _seed_creds()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_account_describe())

    service = _make_service(handler, tmp_cache)
    describe = service.describe(ALIAS, "Account")
    owner = next(f for f in describe.fields if f.name == "OwnerId")
    assert owner.type == "reference"
    assert owner.reference_to == ("User", "Group")
    assert owner.relationship_name == "Owner"


def test_describe_uses_per_org_cache_keys(fake_keyring: FakeKeyring, tmp_cache: JsonCache) -> None:
    instance_a = "https://a.my.salesforce.com"
    instance_b = "https://b.my.salesforce.com"
    config = Config(
        orgs=[
            _entry("a", instance_url=instance_a),
            _entry("b", instance_url=instance_b),
        ]
    )
    _seed_creds("a", instance_url=instance_a)
    _seed_creds("b", instance_url=instance_b)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # Return different labels per org so we can tell them apart.
        payload = _account_describe()
        host = request.url.host
        payload["label"] = f"Account@{host}"
        return httpx.Response(200, json=payload)

    service = _make_service(handler, tmp_cache, config=config)
    a = service.describe("a", "Account")
    b = service.describe("b", "Account")

    assert a.label == "Account@a.my.salesforce.com"
    assert b.label == "Account@b.my.salesforce.com"
    # Both fetched from the network — cache must not have crossed orgs.
    assert len(calls) == 2


def test_describe_falls_through_on_corrupt_cache_file(
    fake_keyring: FakeKeyring, tmp_cache: JsonCache
) -> None:
    _seed_creds()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_account_describe())

    service = _make_service(handler, tmp_cache)
    # Pre-write garbage to the path describe() will look at.
    path = tmp_cache.path_for(service._key_describe(_entry(), "Account"))  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid", encoding="utf-8")

    describe = service.describe(ALIAS, "Account")

    assert describe.name == "Account"
    assert len(calls) == 1
    assert path.exists()  # rewritten by the fresh fetch
