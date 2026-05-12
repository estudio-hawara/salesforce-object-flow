"""Renderer tests: ``{{col}}`` substitution + ``@{ref.path}`` resolution."""

from __future__ import annotations

from typing import Any

from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.core.serial import (
    BodyField,
    HttpMethod,
    SerialStep,
)
from salesforce_object_flow.services.serial import (
    RenderRow,
    SerialStepRenderer,
    StepResult,
    evaluate_check,
    evaluate_condition,
    navigate_path,
)


def _fmt() -> FileFormat:
    return FileFormat(
        name="r",
        columns=[
            Column(name="email", type=ColumnType.EMAIL),
            Column(name="telephone", type=ColumnType.STRING),
        ],
    )


def test_csv_placeholder_is_substituted() -> None:
    step = SerialStep(
        reference_id="Update",
        method=HttpMethod.PATCH,
        url="/x/{{email}}",
        body=[BodyField(field="Phone", value="{{telephone}}")],
    )
    rendered = SerialStepRenderer().render_step(
        step,
        _fmt(),
        RenderRow(values={"email": "a@b.com", "telephone": "34611"}),
        prior={},
    )
    assert rendered.url == "/x/a@b.com"
    assert rendered.body == {"Phone": "34611"}


def test_reference_path_is_resolved_against_prior_result() -> None:
    step = SerialStep(
        reference_id="Update",
        method=HttpMethod.PATCH,
        url="/services/data/v63.0/sobjects/Contact/@{Query.records[0].Id}",
        body=[BodyField(field="Phone", value="{{telephone}}")],
    )
    prior = {
        "Query": StepResult(
            reference_id="Query",
            status="success",
            http_status=200,
            body={"records": [{"Id": "003abc"}]},
            error_summary=None,
        )
    }
    rendered = SerialStepRenderer().render_step(
        step,
        _fmt(),
        RenderRow(values={"email": "a@b.com", "telephone": "34611"}),
        prior=prior,
    )
    assert rendered.url == "/services/data/v63.0/sobjects/Contact/003abc"


def test_unresolved_reference_renders_empty() -> None:
    step = SerialStep(
        reference_id="X",
        method=HttpMethod.POST,
        url="/x/@{Missing.id}",
        body=None,
    )
    rendered = SerialStepRenderer().render_step(
        step,
        _fmt(),
        RenderRow(values={"email": "", "telephone": ""}),
        prior={},
    )
    assert rendered.url == "/x/"


def test_status_outputs_in_reference() -> None:
    step = SerialStep(
        reference_id="X",
        method=HttpMethod.POST,
        url="/log/@{Prev.status}/@{Prev.ok}",
        body=None,
    )
    prior = {
        "Prev": StepResult(
            reference_id="Prev",
            status="success",
            http_status=201,
            body={"id": "1"},
            error_summary=None,
        )
    }
    rendered = SerialStepRenderer().render_step(
        step,
        _fmt(),
        RenderRow(values={"email": "", "telephone": ""}),
        prior=prior,
    )
    assert rendered.url == "/log/201/true"


def test_navigate_path_handles_indexes_and_dots() -> None:
    blob: Any = {"records": [{"Id": "1"}, {"Id": "2", "nested": {"x": 9}}]}
    assert navigate_path(blob, "records[1].Id") == "2"
    assert navigate_path(blob, "records[1].nested.x") == 9
    assert navigate_path(blob, "records[5].Id") is None
    assert navigate_path(blob, "missing") is None


def test_evaluate_condition_empty_returns_true() -> None:
    assert evaluate_condition(None, {}) is True


def test_evaluate_check_status_ok_on_skipped_is_false() -> None:
    prior = {
        "A": StepResult(
            reference_id="A",
            status="skipped",
            http_status=0,
            body=None,
            error_summary=None,
        )
    }
    from salesforce_object_flow.core.serial import CheckOp, ConditionCheck

    assert evaluate_check(ConditionCheck(op=CheckOp.STATUS_OK, ref="A"), prior) is False
    assert evaluate_check(ConditionCheck(op=CheckOp.STATUS_FAILED, ref="A"), prior) is False


def test_records_count_ops_consume_int_value() -> None:
    from salesforce_object_flow.core.serial import CheckOp, ConditionCheck

    prior = {
        "Q": StepResult(
            reference_id="Q",
            status="success",
            http_status=200,
            body={"records": [{"Id": "x"}]},
            error_summary=None,
        )
    }
    assert evaluate_check(
        ConditionCheck(op=CheckOp.RECORDS_COUNT_EQ, ref="Q", path="records", value="1"),
        prior,
    )
    assert evaluate_check(
        ConditionCheck(op=CheckOp.RECORDS_COUNT_GT, ref="Q", path="records", value="0"),
        prior,
    )
    assert not evaluate_check(
        ConditionCheck(op=CheckOp.RECORDS_COUNT_EQ, ref="Q", path="records", value="2"),
        prior,
    )
