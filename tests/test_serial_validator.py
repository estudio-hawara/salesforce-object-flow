"""Validation tests for ``services/serial.py:SerialDefinitionValidator``."""

from __future__ import annotations

from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.core.serial import (
    BodyField,
    CheckOp,
    ConditionCheck,
    HttpMethod,
    SerialDefinition,
    SerialStep,
    StepCondition,
)
from salesforce_object_flow.services.serial import SerialDefinitionValidator


def _fmt() -> FileFormat:
    return FileFormat(
        name="redes",
        columns=[
            Column(name="email", type=ColumnType.EMAIL),
            Column(name="telephone", type=ColumnType.STRING),
        ],
    )


def _step(**overrides: object) -> SerialStep:
    base: dict[str, object] = {
        "reference_id": "Q",
        "method": HttpMethod.GET,
        "url": "/services/data/v63.0/query/?q=SELECT+Id+FROM+Contact",
        "body": None,
        "headers": {},
        "condition": None,
        "continue_on_failure": False,
    }
    base.update(overrides)
    return SerialStep(**base)  # type: ignore[arg-type]


def test_valid_definition_reports_no_errors() -> None:
    definition = SerialDefinition(
        name="ok",
        format_filename="redes.json",
        steps=[_step()],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert report.ok


def test_empty_name_reports_error() -> None:
    definition = SerialDefinition(name="", format_filename="redes.json", steps=[_step()])
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert not report.ok
    assert any(err.field == "name" for err in report.errors)


def test_missing_format_link() -> None:
    definition = SerialDefinition(name="x", format_filename="", steps=[_step()])
    report = SerialDefinitionValidator().validate(definition, None)
    assert any(err.field == "format_filename" for err in report.errors)


def test_at_least_one_step_required() -> None:
    definition = SerialDefinition(name="x", format_filename="redes.json", steps=[])
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "steps" for err in report.errors)


def test_unknown_placeholder_is_flagged() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(url="/x/{{not_a_column}}"),
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any("not_a_column" in err.message for err in report.errors)


def test_forward_reference_is_rejected() -> None:
    # B references A, but A is defined after B → forward ref.
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(reference_id="B", url="/x/@{A.id}"),
            _step(reference_id="A"),
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "url" and err.step_index == 0 for err in report.errors)


def test_backwards_reference_is_accepted() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(reference_id="A"),
            _step(reference_id="B", url="/x/@{A.id}"),
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert report.ok


def test_condition_check_must_reference_previous_step() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(
                reference_id="A",
                condition=StepCondition(
                    checks=[ConditionCheck(op=CheckOp.STATUS_OK, ref="B")]
                ),
            ),
            _step(reference_id="B"),
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "condition.ref" for err in report.errors)


def test_records_count_op_requires_integer_value() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(reference_id="A"),
            _step(
                reference_id="B",
                condition=StepCondition(
                    checks=[
                        ConditionCheck(
                            op=CheckOp.RECORDS_COUNT_EQ,
                            ref="A",
                            path="records",
                            value="zero",
                        )
                    ]
                ),
            ),
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "condition.value" for err in report.errors)


def test_body_on_get_method_is_flagged() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[
            _step(
                method=HttpMethod.GET,
                url="/x",
                body=[BodyField(field="Foo", value="bar")],
            )
        ],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "body" for err in report.errors)


def test_invalid_reference_id_pattern_is_flagged() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[_step(reference_id="1-bad")],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert any(err.field == "reference_id" for err in report.errors)


def test_duplicate_reference_ids_flagged() -> None:
    definition = SerialDefinition(
        name="x",
        format_filename="redes.json",
        steps=[_step(reference_id="A"), _step(reference_id="A")],
    )
    report = SerialDefinitionValidator().validate(definition, _fmt())
    assert sum(err.field == "reference_id" for err in report.errors) >= 2
