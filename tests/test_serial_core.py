"""Round-trip + tolerant-parse tests for ``core/serial.py``."""

from __future__ import annotations

from typing import Any

from salesforce_object_flow.core.serial import (
    BodyField,
    CheckOp,
    ConditionCheck,
    ConditionCombinator,
    HttpMethod,
    SerialDefinition,
    SerialStep,
    StepCondition,
)


def _full_definition() -> SerialDefinition:
    return SerialDefinition(
        name="Import contacts",
        description="Conditional create-or-update plus membership.",
        format_filename="redes.json",
        steps=[
            SerialStep(
                reference_id="ContactQuery",
                method=HttpMethod.GET,
                url="/services/data/v63.0/query/?q=SELECT+Id+FROM+Contact",
            ),
            SerialStep(
                reference_id="ContactCreate",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/Contact",
                body=[
                    BodyField(field="FirstName", value="{{firstname}}"),
                    BodyField(field="Email", value="{{email}}"),
                ],
                condition=StepCondition(
                    combinator=ConditionCombinator.ALL_OF,
                    checks=[
                        ConditionCheck(
                            op=CheckOp.RECORDS_COUNT_EQ,
                            ref="ContactQuery",
                            path="records",
                            value="0",
                        )
                    ],
                ),
                continue_on_failure=False,
            ),
            SerialStep(
                reference_id="MemberCreate",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/CampaignMember",
                body=[BodyField(field="ContactId", value="@{ContactCreate.id}")],
                condition=StepCondition(
                    combinator=ConditionCombinator.ANY_OF,
                    checks=[
                        ConditionCheck(op=CheckOp.STATUS_OK, ref="ContactCreate"),
                    ],
                ),
                continue_on_failure=True,
            ),
        ],
    )


def test_serial_definition_round_trip() -> None:
    original = _full_definition()
    encoded = original.to_dict()
    restored = SerialDefinition.from_dict(encoded)
    assert restored is not None
    assert restored == original


def test_step_condition_defaults() -> None:
    cond = StepCondition.from_dict({})
    assert cond is not None
    assert cond.combinator is ConditionCombinator.ALL_OF
    assert cond.checks == []


def test_unknown_combinator_falls_back_to_all_of() -> None:
    cond = StepCondition.from_dict({"combinator": "weird", "checks": []})
    assert cond is not None
    assert cond.combinator is ConditionCombinator.ALL_OF


def test_check_with_unknown_op_is_dropped() -> None:
    cond = StepCondition.from_dict(
        {
            "combinator": "all_of",
            "checks": [
                {"op": "does_not_exist", "ref": "X"},
                {"op": "exists", "ref": "Y", "path": "id"},
            ],
        }
    )
    assert cond is not None
    assert len(cond.checks) == 1
    assert cond.checks[0].op is CheckOp.EXISTS


def test_step_with_unknown_method_is_dropped() -> None:
    step = SerialStep.from_dict(
        {
            "reference_id": "X",
            "method": "TRACE",
            "url": "/x",
        }
    )
    assert step is None


def test_empty_name_definition_is_dropped() -> None:
    assert SerialDefinition.from_dict({"name": "   ", "steps": []}) is None


def test_malformed_steps_are_skipped_not_fatal() -> None:
    payload: dict[str, Any] = {
        "name": "D",
        "steps": [
            {"reference_id": "", "method": "GET", "url": "/a"},  # empty ref → dropped
            {"reference_id": "Good", "method": "GET", "url": "/b"},
        ],
    }
    definition = SerialDefinition.from_dict(payload)
    assert definition is not None
    assert len(definition.steps) == 1
    assert definition.steps[0].reference_id == "Good"


def test_check_value_is_preserved_as_string() -> None:
    check = ConditionCheck.from_dict(
        {"op": "records_count_eq", "ref": "Q", "path": "records", "value": "0"}
    )
    assert check is not None
    assert check.value == "0"
