"""Tests for ``services/composite.py:CompositePayloadRenderer``."""

from __future__ import annotations

from salesforce_object_flow.core.composite import (
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.services.composite import (
    CompositePayloadRenderer,
    RenderRow,
)

RENDERER = CompositePayloadRenderer()


def _fmt(*column_specs: tuple[str, ColumnType]) -> FileFormat:
    return FileFormat(
        name="customer",
        columns=[Column(name=name, type=type_) for name, type_ in column_specs],
    )


def _tpl_with_body(body: list[BodyField] | None, **sub_overrides: object) -> CompositeTemplate:
    sub_kwargs: dict[str, object] = {
        "reference_id": "first",
        "method": HttpMethod.POST,
        "url": "/x",
        "body": body,
        "headers": {},
    }
    sub_kwargs.update(sub_overrides)
    return CompositeTemplate(
        name="T",
        format_filename="customer.json",
        subrequests=[Subrequest(**sub_kwargs)],  # type: ignore[arg-type]
    )


def _body(tpl: CompositeTemplate, fmt: FileFormat, row: RenderRow) -> object:
    rendered = RENDERER.render(tpl, fmt, row)
    return rendered["compositeRequest"][0]["body"]


def test_placeholder_only_integer_returns_int() -> None:
    fmt = _fmt(("age", ColumnType.INTEGER))
    tpl = _tpl_with_body([BodyField(field="Age", value="{{age}}")])
    assert _body(tpl, fmt, RenderRow(values={"age": "42"})) == {"Age": 42}


def test_placeholder_only_boolean_returns_bool() -> None:
    fmt = _fmt(("vip", ColumnType.BOOLEAN))
    tpl = _tpl_with_body([BodyField(field="Vip", value="{{vip}}")])
    assert _body(tpl, fmt, RenderRow(values={"vip": "TRUE"})) == {"Vip": True}


def test_placeholder_only_decimal_returns_float() -> None:
    fmt = _fmt(("amount", ColumnType.DECIMAL))
    tpl = _tpl_with_body([BodyField(field="Amount", value="{{amount}}")])
    assert _body(tpl, fmt, RenderRow(values={"amount": "3.14"})) == {"Amount": 3.14}


def test_placeholder_only_date_returns_iso_string() -> None:
    fmt = _fmt(("birth", ColumnType.DATE))
    tpl = _tpl_with_body([BodyField(field="Birth", value="{{birth}}")])
    assert _body(tpl, fmt, RenderRow(values={"birth": "1999-12-31"})) == {"Birth": "1999-12-31"}


def test_placeholder_only_datetime_returns_iso_string() -> None:
    fmt = _fmt(("ts", ColumnType.DATETIME))
    tpl = _tpl_with_body([BodyField(field="Ts", value="{{ts}}")])
    assert _body(tpl, fmt, RenderRow(values={"ts": "1999-12-31T00:00:00"})) == {
        "Ts": "1999-12-31T00:00:00"
    }


def test_placeholder_only_string_returns_string() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    tpl = _tpl_with_body([BodyField(field="Name", value="{{name}}")])
    assert _body(tpl, fmt, RenderRow(values={"name": "Alice"})) == {"Name": "Alice"}


def test_placeholder_with_prefix_and_suffix_is_string() -> None:
    fmt = _fmt(("age", ColumnType.INTEGER))
    tpl = _tpl_with_body([BodyField(field="Greeting", value="Hi #{{age}} there")])
    assert _body(tpl, fmt, RenderRow(values={"age": "42"})) == {"Greeting": "Hi #42 there"}


def test_literal_value_left_as_string() -> None:
    fmt = _fmt()
    tpl = _tpl_with_body([BodyField(field="Status", value="Active")])
    assert _body(tpl, fmt, RenderRow(values={})) == {"Status": "Active"}


def test_at_reference_left_intact() -> None:
    fmt = _fmt()
    tpl = _tpl_with_body([BodyField(field="AccountId", value="@{newAccount.id}")])
    assert _body(tpl, fmt, RenderRow(values={})) == {"AccountId": "@{newAccount.id}"}


def test_multiple_body_fields_assembled() -> None:
    fmt = _fmt(("name", ColumnType.STRING), ("age", ColumnType.INTEGER))
    tpl = _tpl_with_body(
        [
            BodyField(field="Name", value="{{name}}"),
            BodyField(field="Age__c", value="{{age}}"),
        ]
    )
    assert _body(tpl, fmt, RenderRow(values={"name": "Alice", "age": "33"})) == {
        "Name": "Alice",
        "Age__c": 33,
    }


def test_url_with_placeholder_concatenates_to_string() -> None:
    fmt = _fmt(("id", ColumnType.INTEGER))
    sub = Subrequest(
        reference_id="first",
        method=HttpMethod.PATCH,
        url="/services/data/v63.0/sobjects/Account/{{id}}",
        body=None,
    )
    tpl = CompositeTemplate(name="T", format_filename="customer.json", subrequests=[sub])
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={"id": "42"}))
    assert rendered["compositeRequest"][0]["url"] == "/services/data/v63.0/sobjects/Account/42"


def test_query_url_supports_placeholder() -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    sub = Subrequest(
        reference_id="first",
        method=HttpMethod.GET,
        url="/services/data/v63.0/query?q=SELECT+Id+FROM+Contact+WHERE+Name='{{name}}'",
        body=None,
    )
    tpl = CompositeTemplate(name="T", format_filename="customer.json", subrequests=[sub])
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={"name": "Alice"}))
    assert (
        rendered["compositeRequest"][0]["url"]
        == "/services/data/v63.0/query?q=SELECT+Id+FROM+Contact+WHERE+Name='Alice'"
    )


def test_header_with_placeholder_concatenates_to_string() -> None:
    fmt = _fmt(("ext", ColumnType.STRING))
    sub = Subrequest(
        reference_id="first",
        method=HttpMethod.POST,
        url="/x",
        body=None,
        headers={"X-Ext": "v-{{ext}}"},
    )
    tpl = CompositeTemplate(name="T", format_filename="customer.json", subrequests=[sub])
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={"ext": "abc"}))
    assert rendered["compositeRequest"][0]["httpHeaders"] == {"X-Ext": "v-abc"}


def test_top_level_camel_case_keys() -> None:
    fmt = _fmt()
    tpl = CompositeTemplate(
        name="T",
        format_filename="customer.json",
        all_or_none=False,
        collate_subrequests=True,
        subrequests=[
            Subrequest(
                reference_id="a",
                method=HttpMethod.POST,
                url="/x",
                body=[BodyField(field="X", value="y")],
            ),
        ],
    )
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={}))
    assert set(rendered.keys()) == {"allOrNone", "collateSubrequests", "compositeRequest"}
    assert rendered["allOrNone"] is False
    assert rendered["collateSubrequests"] is True
    sub_keys = set(rendered["compositeRequest"][0].keys())
    assert sub_keys == {"method", "url", "referenceId", "body"}


def test_body_none_omitted_from_subrequest() -> None:
    fmt = _fmt()
    sub = Subrequest(
        reference_id="first",
        method=HttpMethod.GET,
        url="/services/data/v63.0/query",
        body=None,
    )
    tpl = CompositeTemplate(name="T", format_filename="customer.json", subrequests=[sub])
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={}))
    assert "body" not in rendered["compositeRequest"][0]


def test_empty_body_list_omitted_from_subrequest() -> None:
    fmt = _fmt()
    sub = Subrequest(
        reference_id="first",
        method=HttpMethod.POST,
        url="/x",
        body=[],
    )
    tpl = CompositeTemplate(name="T", format_filename="customer.json", subrequests=[sub])
    rendered = RENDERER.render(tpl, fmt, RenderRow(values={}))
    assert "body" not in rendered["compositeRequest"][0]


def test_synthetic_row_has_one_entry_per_column() -> None:
    fmt = _fmt(
        ("a", ColumnType.STRING),
        ("b", ColumnType.INTEGER),
        ("c", ColumnType.BOOLEAN),
    )
    row = CompositePayloadRenderer.synthetic_row(fmt)
    assert set(row.values.keys()) == {"a", "b", "c"}
    assert row.values["b"] == "0"
    assert row.values["c"] == "false"


def test_synthetic_row_for_format_with_no_columns() -> None:
    row = CompositePayloadRenderer.synthetic_row(_fmt())
    assert row.values == {}


def test_failed_coercion_falls_back_to_string() -> None:
    fmt = _fmt(("age", ColumnType.INTEGER))
    tpl = _tpl_with_body([BodyField(field="Age", value="{{age}}")])
    assert _body(tpl, fmt, RenderRow(values={"age": "abc"})) == {"Age": "abc"}
