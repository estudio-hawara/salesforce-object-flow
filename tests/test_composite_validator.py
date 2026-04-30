"""Tests for ``services/composite.py:CompositeTemplateValidator``."""

from __future__ import annotations

from salesforce_object_flow.core.composite import (
    MAX_SUBREQUESTS,
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.services.composite import (
    CompositeTemplateValidator,
    TemplateError,
)


def _fmt(*column_specs: tuple[str, ColumnType]) -> FileFormat:
    return FileFormat(
        name="customer",
        columns=[Column(name=name, type=type_) for name, type_ in column_specs],
    )


def _sub(reference_id: str = "first", **overrides: object) -> Subrequest:
    defaults: dict[str, object] = {
        "reference_id": reference_id,
        "method": HttpMethod.POST,
        "url": "/services/data/v63.0/sobjects/Account",
        "body": None,
        "headers": {},
    }
    defaults.update(overrides)
    return Subrequest(**defaults)  # type: ignore[arg-type]


def _tpl(name: str = "T", **overrides: object) -> CompositeTemplate:
    defaults: dict[str, object] = {
        "name": name,
        "format_filename": "customer.json",
        "subrequests": [_sub()],
    }
    defaults.update(overrides)
    return CompositeTemplate(**defaults)  # type: ignore[arg-type]


VALIDATOR = CompositeTemplateValidator()


def _has(errors: tuple[TemplateError, ...], *, field: str, message_substr: str = "") -> bool:
    return any(
        e.field == field and (message_substr in e.message if message_substr else True)
        for e in errors
    )


# ---- Template-level shape ------------------------------------------------


def test_empty_name() -> None:
    tpl = _tpl(name="")
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="name", message_substr="required")


def test_empty_format_filename() -> None:
    tpl = _tpl(format_filename="")
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="format_filename", message_substr="Format link")


def test_zero_subrequests() -> None:
    tpl = _tpl(subrequests=[])
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="subrequests", message_substr="At least one")


def test_too_many_subrequests() -> None:
    subs = [_sub(reference_id=f"r{i}") for i in range(MAX_SUBREQUESTS + 1)]
    tpl = _tpl(subrequests=subs)
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="subrequests", message_substr="Maximum 25")


def test_duplicate_reference_id() -> None:
    tpl = _tpl(subrequests=[_sub("a"), _sub("a"), _sub("b")])
    report = VALIDATOR.validate(tpl, _fmt())
    duplicate_errors = [
        e for e in report.errors if e.field == "reference_id" and "Duplicate" in e.message
    ]
    indices = sorted(e.subrequest_index for e in duplicate_errors)
    assert indices == [0, 1]


def test_format_missing() -> None:
    tpl = _tpl(format_filename="ghost.json")
    report = VALIDATOR.validate(tpl, None)
    assert _has(report.errors, field="format_filename", message_substr='"ghost.json" not found')


# ---- Per-subrequest shape ------------------------------------------------


def test_invalid_reference_id() -> None:
    tpl = _tpl(subrequests=[_sub("1bad")])
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="reference_id", message_substr="Invalid")


def test_empty_url() -> None:
    tpl = _tpl(subrequests=[_sub(url="")])
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="url", message_substr="required")


def test_url_not_starting_with_slash() -> None:
    tpl = _tpl(subrequests=[_sub(url="services/data/v63.0/sobjects/Account")])
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="url", message_substr='start with "/"')


def test_get_with_body_errors() -> None:
    tpl = _tpl(
        subrequests=[
            _sub(
                method=HttpMethod.GET,
                url="/services/data/v63.0/query",
                body=[BodyField(field="q", value="x")],
            )
        ]
    )
    report = VALIDATOR.validate(tpl, _fmt())
    assert _has(report.errors, field="body", message_substr="not allowed for GET")


def test_delete_with_body_none_ok() -> None:
    tpl = _tpl(
        subrequests=[_sub(method=HttpMethod.DELETE, url="/services/data/v63.0/sobjects/Account/X")]
    )
    report = VALIDATOR.validate(tpl, _fmt())
    assert report.ok


def test_post_with_body_none_ok() -> None:
    tpl = _tpl(subrequests=[_sub(body=None)])
    report = VALIDATOR.validate(tpl, _fmt())
    assert report.ok


def test_post_with_empty_body_ok() -> None:
    tpl = _tpl(subrequests=[_sub(body=[])])
    report = VALIDATOR.validate(tpl, _fmt())
    assert report.ok


def test_body_with_empty_field_name_errors() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub(body=[BodyField(field="  ", value="x")])])
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="body", message_substr="Body field name is required")


def test_body_with_duplicate_field_errors() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(
        subrequests=[
            _sub(
                body=[
                    BodyField(field="Name", value="a"),
                    BodyField(field="Name", value="b"),
                ]
            )
        ]
    )
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="body", message_substr='Duplicate body field "Name"')


# ---- Placeholders --------------------------------------------------------


def test_known_placeholder_in_body_value() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub(body=[BodyField(field="Name", value="{{name}}")])])
    report = VALIDATOR.validate(tpl, fmt)
    assert report.ok


def test_unknown_placeholder_in_body_value() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub(body=[BodyField(field="Name", value="{{ghost}}")])])
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="body", message_substr='Unknown column "ghost"')


def test_unknown_placeholder_in_url() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub(url="/services/data/v63.0/sobjects/Account/{{ghost}}")])
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="url", message_substr='"ghost"')


def test_placeholder_in_query_url_supported() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(
        subrequests=[
            _sub(
                method=HttpMethod.GET,
                url="/services/data/v63.0/query?q=SELECT+Id+FROM+Contact+WHERE+Name='{{name}}'",
                body=None,
            )
        ]
    )
    report = VALIDATOR.validate(tpl, fmt)
    assert report.ok


def test_unknown_placeholder_in_header() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub(headers={"X-Tag": "{{ghost}}"})])
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="headers", message_substr='"ghost"')


def test_placeholder_check_skipped_when_format_missing() -> None:
    tpl = _tpl(subrequests=[_sub(body=[BodyField(field="Name", value="{{ghost}}")])])
    report = VALIDATOR.validate(tpl, None)
    placeholder_errors = [e for e in report.errors if "Unknown column" in e.message]
    assert placeholder_errors == []


# ---- References ----------------------------------------------------------


def test_known_reference_passes() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(
        subrequests=[
            _sub("newAccount"),
            _sub(
                "newContact",
                body=[BodyField(field="AccountId", value="@{newAccount.id}")],
            ),
        ]
    )
    report = VALIDATOR.validate(tpl, fmt)
    assert report.ok


def test_unknown_reference_errors() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(subrequests=[_sub("a", body=[BodyField(field="X", value="@{ghost.id}")])])
    report = VALIDATOR.validate(tpl, fmt)
    assert _has(report.errors, field="body", message_substr='"@{ghost.…}"')


def test_forward_reference_allowed() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl(
        subrequests=[
            _sub("first", body=[BodyField(field="Next", value="@{second.id}")]),
            _sub("second"),
        ]
    )
    report = VALIDATOR.validate(tpl, fmt)
    assert report.ok
