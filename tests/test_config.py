"""Round-trip tests for the platformdirs-backed JSON config."""

from __future__ import annotations

import json
from pathlib import Path

from salesforce_object_flow.core.config import Config


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "nonexistent.json")
    assert config == Config()


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    Config(auto_save=True, last_org_alias="prod").save(target)

    loaded = Config.load(target)
    assert loaded.auto_save is True
    assert loaded.last_org_alias == "prod"


def test_unknown_keys_are_dropped(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"auto_save": True, "removed_field": 42, "future_field": "x"}),
        encoding="utf-8",
    )

    loaded = Config.load(target)
    assert loaded.auto_save is True
    assert loaded.last_org_alias is None


def test_invalid_json_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("not valid json {{", encoding="utf-8")

    assert Config.load(target) == Config()


def test_non_object_root_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert Config.load(target) == Config()
