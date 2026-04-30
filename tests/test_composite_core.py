"""Tests for ``core/composite.py``."""

from __future__ import annotations

from salesforce_object_flow.core.composite import (
    REFERENCE_ID_RE,
    SCHEMA_VERSION,
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)


def _sub(reference_id: str = "first", **overrides: object) -> Subrequest:
    defaults: dict[str, object] = {
        "reference_id": reference_id,
        "method": HttpMethod.POST,
        "url": "/services/data/v63.0/sobjects/Account",
        "body": [BodyField(field="Name", value="{{name}}")],
        "headers": {},
    }
    defaults.update(overrides)
    return Subrequest(**defaults)  # type: ignore[arg-type]


def test_round_trip_minimal() -> None:
    tpl = CompositeTemplate(
        name="x",
        format_filename="customer.json",
        subrequests=[_sub()],
    )
    parsed = CompositeTemplate.from_dict(tpl.to_dict())
    assert parsed is not None
    assert parsed == tpl


def test_round_trip_full() -> None:
    tpl = CompositeTemplate(
        name="Account + Contact",
        description="Two-step create",
        format_filename="customer-extract.json",
        all_or_none=False,
        collate_subrequests=True,
        subrequests=[
            _sub(
                reference_id="newAccount",
                body=[
                    BodyField(field="Name", value="{{company}}"),
                    BodyField(field="Tag__c", value="{{tag}}"),
                ],
                headers={"Sforce-Auto-Assign": "false"},
            ),
            _sub(
                reference_id="newContact",
                body=[
                    BodyField(field="AccountId", value="@{newAccount.id}"),
                    BodyField(field="Email", value="{{email}}"),
                ],
            ),
        ],
    )
    payload = tpl.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION

    parsed = CompositeTemplate.from_dict(payload)
    assert parsed is not None
    assert parsed == tpl


def test_from_dict_drops_unknown_keys() -> None:
    payload = {
        "name": "x",
        "format_filename": "f.json",
        "future_field": "ignored",
        "subrequests": [],
    }
    parsed = CompositeTemplate.from_dict(payload)
    assert parsed is not None
    assert parsed.name == "x"


def test_from_dict_returns_none_on_missing_name() -> None:
    assert CompositeTemplate.from_dict({"description": "no name"}) is None


def test_from_dict_returns_none_on_empty_name() -> None:
    assert CompositeTemplate.from_dict({"name": "   "}) is None


def test_from_dict_subrequests_non_list_yields_empty() -> None:
    parsed = CompositeTemplate.from_dict({"name": "x", "subrequests": "nope"})
    assert parsed is not None
    assert parsed.subrequests == []


def test_from_dict_drops_subrequests_with_unknown_method() -> None:
    payload = {
        "name": "x",
        "subrequests": [
            {
                "reference_id": "a",
                "method": "OPTIONS",
                "url": "/x",
            },
            {
                "reference_id": "b",
                "method": "POST",
                "url": "/y",
            },
        ],
    }
    parsed = CompositeTemplate.from_dict(payload)
    assert parsed is not None
    assert [s.reference_id for s in parsed.subrequests] == ["b"]


def test_subrequest_from_dict_rejects_lowercase_method() -> None:
    parsed = Subrequest.from_dict({"reference_id": "a", "method": "post", "url": "/x"})
    assert parsed is None


def test_subrequest_from_dict_rejects_empty_reference_id() -> None:
    parsed = Subrequest.from_dict({"reference_id": "  ", "method": "POST", "url": "/x"})
    assert parsed is None


def test_subrequest_from_dict_drops_non_dict_headers() -> None:
    parsed = Subrequest.from_dict(
        {
            "reference_id": "a",
            "method": "POST",
            "url": "/x",
            "headers": "not a dict",
        }
    )
    assert parsed is not None
    assert parsed.headers == {}


def test_to_dict_always_includes_schema_version_and_empty_headers() -> None:
    tpl = CompositeTemplate(
        name="x",
        subrequests=[_sub()],
    )
    payload = tpl.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["subrequests"][0]["headers"] == {}


def test_reference_id_regex() -> None:
    assert REFERENCE_ID_RE.match("a") is not None
    assert REFERENCE_ID_RE.match("newAccount") is not None
    assert REFERENCE_ID_RE.match("ref_1") is not None
    assert REFERENCE_ID_RE.match("1abc") is None
    assert REFERENCE_ID_RE.match("_x") is None
    assert REFERENCE_ID_RE.match("with space") is None
