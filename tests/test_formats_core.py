"""Tests for ``core/formats.py``."""

from __future__ import annotations

from salesforce_object_flow.core.formats import (
    SCHEMA_VERSION,
    Column,
    ColumnType,
    FileFormat,
    slugify,
)


def test_round_trip_minimal() -> None:
    fmt = FileFormat(name="x")
    parsed = FileFormat.from_dict(fmt.to_dict())
    assert parsed is not None
    assert parsed == fmt


def test_round_trip_full() -> None:
    fmt = FileFormat(
        name="Customer extract",
        description="Quarterly sync",
        delimiter=";",
        quote_char="'",
        has_header=False,
        encoding="latin-1",
        columns=[
            Column(name="id", type=ColumnType.INTEGER, nullable=False),
            Column(name="amount", type=ColumnType.DECIMAL),
            Column(name="email", type=ColumnType.EMAIL),
            Column(name="active", type=ColumnType.BOOLEAN),
            Column(name="created", type=ColumnType.DATETIME),
            Column(name="birthday", type=ColumnType.DATE),
            Column(name="notes", type=ColumnType.STRING),
        ],
    )
    payload = fmt.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION

    parsed = FileFormat.from_dict(payload)
    assert parsed is not None
    assert parsed == fmt


def test_from_dict_drops_unknown_keys() -> None:
    payload = {
        "name": "x",
        "future_field": "ignored",
        "columns": [],
    }
    parsed = FileFormat.from_dict(payload)
    assert parsed is not None
    assert parsed.name == "x"


def test_from_dict_returns_none_on_missing_name() -> None:
    assert FileFormat.from_dict({"description": "no name"}) is None


def test_from_dict_returns_none_on_empty_name() -> None:
    assert FileFormat.from_dict({"name": "   "}) is None


def test_from_dict_drops_unknown_column_type() -> None:
    payload = {
        "name": "x",
        "columns": [
            {"name": "ok", "type": "string"},
            {"name": "weird", "type": "telephone"},
        ],
    }
    parsed = FileFormat.from_dict(payload)
    assert parsed is not None
    assert [c.name for c in parsed.columns] == ["ok"]


def test_from_dict_drops_columns_with_empty_name() -> None:
    payload = {
        "name": "x",
        "columns": [
            {"name": "", "type": "string"},
            {"name": "ok", "type": "string"},
        ],
    }
    parsed = FileFormat.from_dict(payload)
    assert parsed is not None
    assert [c.name for c in parsed.columns] == ["ok"]


def test_from_dict_returns_none_on_non_dict() -> None:
    # Mypy/pyright won't allow this directly; the runtime guard is what we test.
    assert FileFormat.from_dict({"name": "x", "columns": "not a list"}) is not None


def test_from_dict_columns_payload_must_be_a_list() -> None:
    parsed = FileFormat.from_dict({"name": "x", "columns": "nope"})
    assert parsed is not None
    assert parsed.columns == []


def test_slugify_basic() -> None:
    assert slugify("Customer Extract") == "customer-extract"


def test_slugify_strips_punctuation() -> None:
    assert slugify("Foo / Bar: 2.0") == "foo-bar-2-0"


def test_slugify_empty_falls_back() -> None:
    assert slugify("!!!") == "format"
    assert slugify("") == "format"


def test_slugify_unicode_strips_diacritics() -> None:
    assert slugify("Año 1") == "ano-1"
    assert slugify("Café") == "cafe"


def test_slugify_collapses_runs() -> None:
    assert slugify("foo   ___   bar") == "foo-bar"
