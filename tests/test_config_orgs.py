"""Tests for the ``orgs`` extension to ``Config``."""

from __future__ import annotations

import json
from pathlib import Path

from salesforce_object_flow.core.config import DEFAULT_API_VERSION, Config, OrgEntry


def _entry(alias: str = "prod", **overrides: object) -> OrgEntry:
    defaults: dict[str, object] = {
        "alias": alias,
        "instance_url": f"https://{alias}.my.salesforce.com",
        "my_domain_url": f"https://{alias}.my.salesforce.com",
        "client_id": f"client-id-{alias}",
        "is_sandbox": False,
        "api_version": DEFAULT_API_VERSION,
    }
    defaults.update(overrides)
    return OrgEntry(**defaults)  # type: ignore[arg-type]


def test_round_trip_with_orgs(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    config = Config(
        active_org_alias="prod",
        orgs=[_entry("prod"), _entry("qa", is_sandbox=True)],
    )
    config.save(target)

    loaded = Config.load(target)

    assert loaded.active_org_alias == "prod"
    assert [o.alias for o in loaded.orgs] == ["prod", "qa"]
    assert loaded.find_org("qa") is not None
    assert loaded.find_org("qa").is_sandbox is True  # type: ignore[union-attr]


def test_load_drops_malformed_org_entries(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "orgs": [
                    {
                        "alias": "ok",
                        "instance_url": "https://ok.my.salesforce.com",
                        "my_domain_url": "https://ok.my.salesforce.com",
                        "client_id": "abc",
                    },
                    {"broken": True},
                    "totally not a dict",
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = Config.load(target)

    assert [o.alias for o in loaded.orgs] == ["ok"]


def test_load_unknown_keys_in_org_entry_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "orgs": [
                    {
                        "alias": "prod",
                        "instance_url": "https://prod.my.salesforce.com",
                        "my_domain_url": "https://prod.my.salesforce.com",
                        "client_id": "abc",
                        "future_field": "ignored",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = Config.load(target)

    assert len(loaded.orgs) == 1
    assert loaded.orgs[0].api_version == DEFAULT_API_VERSION


def test_remove_org_clears_active(tmp_path: Path) -> None:
    config = Config(active_org_alias="prod", orgs=[_entry("prod")])

    assert config.remove_org("prod") is True
    assert config.orgs == []
    assert config.active_org_alias is None


def test_remove_org_keeps_active_when_other_org_removed(tmp_path: Path) -> None:
    config = Config(
        active_org_alias="prod",
        orgs=[_entry("prod"), _entry("qa")],
    )

    config.remove_org("qa")

    assert config.active_org_alias == "prod"


def test_remove_org_returns_false_when_absent() -> None:
    config = Config(orgs=[_entry("prod")])

    assert config.remove_org("does-not-exist") is False


def test_migration_from_last_org_alias(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"auto_save": True, "last_org_alias": "prod"}),
        encoding="utf-8",
    )

    loaded = Config.load(target)

    assert loaded.active_org_alias == "prod"
    assert loaded.last_org_alias == "prod"


def test_active_org_alias_takes_precedence_over_last_org_alias(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"last_org_alias": "old", "active_org_alias": "new"}),
        encoding="utf-8",
    )

    loaded = Config.load(target)

    assert loaded.active_org_alias == "new"


def test_upsert_replaces_by_alias() -> None:
    original = _entry("prod", api_version="v60.0")
    config = Config(orgs=[original])

    updated = _entry("prod", api_version="v63.0")
    config.upsert_org(updated)

    assert len(config.orgs) == 1
    assert config.orgs[0].api_version == "v63.0"


def test_upsert_appends_new_alias() -> None:
    config = Config(orgs=[_entry("prod")])

    config.upsert_org(_entry("qa"))

    assert [o.alias for o in config.orgs] == ["prod", "qa"]


def test_find_org_returns_none_for_missing() -> None:
    config = Config(orgs=[_entry("prod")])

    assert config.find_org("qa") is None
