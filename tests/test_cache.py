"""Tests for the disk-backed JSON cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from salesforce_object_flow.core.cache import CacheKey, JsonCache


def _key(
    *,
    namespace: str = "ns",
    instance_url: str = "https://acme.my.salesforce.com",
    api_version: str = "v63.0",
    extra: str = "",
) -> CacheKey:
    return CacheKey(
        namespace=namespace,
        instance_url=instance_url,
        api_version=api_version,
        extra=extra,
    )


def test_set_get_round_trip(tmp_cache: JsonCache) -> None:
    key = _key(extra="Account")
    tmp_cache.set(key, {"name": "Account", "fields": [{"a": 1}]})

    loaded = tmp_cache.get(key)
    assert loaded == {"name": "Account", "fields": [{"a": 1}]}


def test_get_returns_none_for_missing(tmp_cache: JsonCache) -> None:
    assert tmp_cache.get(_key(extra="Missing")) is None


def test_set_atomic_write_no_partial_file_on_crash(
    tmp_cache: JsonCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _key(extra="Account")
    tmp_cache.set(key, {"first": "value"})
    original_path = tmp_cache.path_for(key)
    original_bytes = original_path.read_bytes()

    real_replace = Path.replace

    def boom(self: Path, target: str | Path) -> Path:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "replace", boom)
    # The cache logs but does not raise; the destination must remain untouched.
    tmp_cache.set(key, {"second": "value"})
    monkeypatch.setattr(Path, "replace", real_replace)

    assert original_path.read_bytes() == original_bytes


def test_corrupt_json_returns_none_and_unlinks(tmp_cache: JsonCache) -> None:
    key = _key(extra="Account")
    path = tmp_cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    assert tmp_cache.get(key) is None
    assert not path.exists()


def test_non_object_payload_is_treated_as_corrupt(tmp_cache: JsonCache) -> None:
    key = _key(extra="Account")
    path = tmp_cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert tmp_cache.get(key) is None
    assert not path.exists()


def test_keys_are_isolated_by_instance_url(tmp_cache: JsonCache) -> None:
    a = _key(instance_url="https://a.my.salesforce.com", extra="Account")
    b = _key(instance_url="https://b.my.salesforce.com", extra="Account")
    tmp_cache.set(a, {"from": "a"})
    tmp_cache.set(b, {"from": "b"})

    assert tmp_cache.get(a) == {"from": "a"}
    assert tmp_cache.get(b) == {"from": "b"}


def test_keys_are_isolated_by_api_version(tmp_cache: JsonCache) -> None:
    a = _key(api_version="v62.0", extra="Account")
    b = _key(api_version="v63.0", extra="Account")
    tmp_cache.set(a, {"v": 62})
    tmp_cache.set(b, {"v": 63})

    assert tmp_cache.get(a) == {"v": 62}
    assert tmp_cache.get(b) == {"v": 63}


def test_delete_namespace_only_clears_matching(tmp_cache: JsonCache) -> None:
    instance_a = "https://a.my.salesforce.com"
    instance_b = "https://b.my.salesforce.com"

    tmp_cache.set(_key(namespace="list", instance_url=instance_a), {"x": 1})
    tmp_cache.set(_key(namespace="describe", instance_url=instance_a, extra="Account"), {"x": 2})
    tmp_cache.set(_key(namespace="describe", instance_url=instance_a, extra="Contact"), {"x": 3})
    tmp_cache.set(_key(namespace="describe", instance_url=instance_b, extra="Account"), {"y": 4})

    removed = tmp_cache.delete_namespace("describe", instance_a, "v63.0")

    assert removed == 2
    # 'describe' for instance_b survives.
    assert tmp_cache.get(_key(namespace="describe", instance_url=instance_b, extra="Account")) == {
        "y": 4
    }
    # 'list' namespace untouched.
    assert tmp_cache.get(_key(namespace="list", instance_url=instance_a)) == {"x": 1}
    # 'describe' entries for instance_a are gone.
    assert (
        tmp_cache.get(_key(namespace="describe", instance_url=instance_a, extra="Account")) is None
    )


def test_delete_clears_individual_entry(tmp_cache: JsonCache) -> None:
    key = _key(extra="Account")
    tmp_cache.set(key, {"x": 1})

    tmp_cache.delete(key)

    assert tmp_cache.get(key) is None


def test_delete_missing_is_noop(tmp_cache: JsonCache) -> None:
    tmp_cache.delete(_key(extra="Nope"))  # must not raise
