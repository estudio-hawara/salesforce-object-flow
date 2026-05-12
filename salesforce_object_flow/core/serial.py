"""Serial definition dataclasses.

Pure data + JSON. GTK-free. A :class:`SerialDefinition` describes a list of
REST steps the client executes in order, one per CSV row, with optional
predicate-based conditions deciding whether each step runs.

Unlike a :class:`core.composite.CompositeTemplate` (one atomic call to
``/services/data/<v>/composite``), a serial definition produces *N*
independent HTTP requests per row. References (``@{ref.path}``) and
predicates are resolved client-side against the parsed response of prior
steps.

Stored on disk as one JSON file per definition under
``platformdirs.user_data_dir / serials/`` (see
:class:`services.serial.SerialDefinitionStore`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, cast

from salesforce_object_flow.core.composite import (
    METHODS_WITH_BODY,
    REFERENCE_ID_RE,
    BodyField,
    HttpMethod,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 1

# Re-export for callers that only depend on this module.
__all__ = [
    "SCHEMA_VERSION",
    "METHODS_WITH_BODY",
    "REFERENCE_ID_RE",
    "BodyField",
    "HttpMethod",
    "CheckOp",
    "ConditionCheck",
    "ConditionCombinator",
    "StepCondition",
    "SerialStep",
    "SerialDefinition",
    "OPS_REQUIRING_PATH",
    "OPS_REQUIRING_VALUE",
    "OPS_NUMERIC_VALUE",
]


class CheckOp(StrEnum):
    """The predicate language used in :class:`ConditionCheck`."""

    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    STATUS_OK = "status_ok"
    STATUS_FAILED = "status_failed"
    RECORDS_COUNT_EQ = "records_count_eq"
    RECORDS_COUNT_GT = "records_count_gt"
    EQ = "eq"
    NE = "ne"


OPS_REQUIRING_PATH: Final[frozenset[CheckOp]] = frozenset(
    {
        CheckOp.EXISTS,
        CheckOp.NOT_EXISTS,
        CheckOp.EQ,
        CheckOp.NE,
        CheckOp.RECORDS_COUNT_EQ,
        CheckOp.RECORDS_COUNT_GT,
    }
)
OPS_REQUIRING_VALUE: Final[frozenset[CheckOp]] = frozenset(
    {CheckOp.EQ, CheckOp.NE, CheckOp.RECORDS_COUNT_EQ, CheckOp.RECORDS_COUNT_GT}
)
OPS_NUMERIC_VALUE: Final[frozenset[CheckOp]] = frozenset(
    {CheckOp.RECORDS_COUNT_EQ, CheckOp.RECORDS_COUNT_GT}
)


class ConditionCombinator(StrEnum):
    """How a :class:`StepCondition` combines its individual checks."""

    ALL_OF = "all_of"
    ANY_OF = "any_of"


@dataclass(slots=True)
class ConditionCheck:
    """One predicate against a previous step's outcome or response body.

    ``ref`` is the ``reference_id`` of an earlier step. ``path`` is a dotted
    JSON path inside that step's parsed response (``records[0].Id``,
    ``id``, …); empty for ops that ignore the body (``STATUS_OK``,
    ``STATUS_FAILED``). ``value`` is a literal string compared against the
    resolved path; ignored for ops that don't need it.
    """

    op: CheckOp
    ref: str
    path: str = ""
    value: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConditionCheck | None:
        try:
            op_value = str(data["op"])
            try:
                op = CheckOp(op_value)
            except ValueError:
                log.warning("Dropping condition check with unknown op: %r", op_value)
                return None
            ref = str(data.get("ref", "")).strip()
            if not ref:
                log.warning("Dropping condition check with empty ref")
                return None
            return cls(
                op=op,
                ref=ref,
                path=str(data.get("path", "")),
                value=str(data.get("value", "")),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed condition check entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op.value,
            "ref": self.ref,
            "path": self.path,
            "value": self.value,
        }


@dataclass(slots=True)
class StepCondition:
    """A boolean combinator over a list of :class:`ConditionCheck` entries.

    An empty ``checks`` list (or ``None`` condition on the step) means the
    step always runs.
    """

    combinator: ConditionCombinator = ConditionCombinator.ALL_OF
    checks: list[ConditionCheck] = field(default_factory=list[ConditionCheck])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepCondition | None:
        try:
            combinator_value = str(data.get("combinator", ConditionCombinator.ALL_OF.value))
            try:
                combinator = ConditionCombinator(combinator_value)
            except ValueError:
                log.warning(
                    "Dropping condition: unknown combinator %r; falling back to all_of",
                    combinator_value,
                )
                combinator = ConditionCombinator.ALL_OF
            raw_checks = data.get("checks", [])
            checks: list[ConditionCheck] = []
            if isinstance(raw_checks, list):
                for raw in cast(list[Any], raw_checks):
                    if isinstance(raw, dict):
                        check = ConditionCheck.from_dict(cast(Mapping[str, Any], raw))
                        if check is not None:
                            checks.append(check)
            return cls(combinator=combinator, checks=checks)
        except (TypeError, ValueError):
            log.warning("Dropping malformed condition entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "combinator": self.combinator.value,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(slots=True)
class SerialStep:
    """One client-driven REST request in a :class:`SerialDefinition`.

    ``condition`` decides whether the step runs at all. ``continue_on_failure``
    controls how the executor handles failures: by default a failed step
    aborts the row; with the flag on, the executor records the failure and
    keeps running subsequent steps (mirroring Salesforce Composite's
    ``allowsFailure``).
    """

    reference_id: str
    method: HttpMethod
    url: str
    body: list[BodyField] | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    condition: StepCondition | None = None
    continue_on_failure: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SerialStep | None:
        try:
            reference_id = str(data["reference_id"]).strip()
            if not reference_id:
                log.warning("Dropping serial step with empty reference_id")
                return None
            method_value = str(data["method"])
            try:
                method = HttpMethod(method_value)
            except ValueError:
                log.warning("Dropping serial step with unknown method: %r", method_value)
                return None
            url = str(data["url"])
            raw_body = data.get("body", None)
            body: list[BodyField] | None
            if raw_body is None:
                body = None
            elif isinstance(raw_body, list):
                parsed: list[BodyField] = []
                for raw in cast(list[Any], raw_body):
                    if isinstance(raw, dict):
                        entry = BodyField.from_dict(cast(Mapping[str, Any], raw))
                        if entry is not None:
                            parsed.append(entry)
                body = parsed
            else:
                log.warning("Dropping serial step with non-list body: %r", raw_body)
                return None
            raw_headers = data.get("headers", {})
            headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                for key, value in cast(dict[str, Any], raw_headers).items():
                    headers[str(key)] = str(value)
            raw_condition = data.get("condition", None)
            condition: StepCondition | None = None
            if isinstance(raw_condition, dict):
                condition = StepCondition.from_dict(cast(Mapping[str, Any], raw_condition))
            return cls(
                reference_id=reference_id,
                method=method,
                url=url,
                body=body,
                headers=headers,
                condition=condition,
                continue_on_failure=bool(data.get("continue_on_failure", False)),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed serial step entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "method": self.method.value,
            "url": self.url,
            "body": ([entry.to_dict() for entry in self.body] if self.body is not None else None),
            "headers": dict(self.headers),
            "condition": self.condition.to_dict() if self.condition is not None else None,
            "continue_on_failure": self.continue_on_failure,
        }


@dataclass(slots=True)
class SerialDefinition:
    """A sequence of client-driven REST steps to run per CSV row."""

    name: str
    description: str = ""
    format_filename: str = ""
    steps: list[SerialStep] = field(default_factory=list[SerialStep])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SerialDefinition | None:
        try:
            name = str(data["name"]).strip()
            if not name:
                log.warning("Dropping serial definition with empty name")
                return None
            raw_steps = data.get("steps", [])
            steps: list[SerialStep] = []
            if isinstance(raw_steps, list):
                for raw in cast(list[Any], raw_steps):
                    if isinstance(raw, dict):
                        step = SerialStep.from_dict(cast(Mapping[str, Any], raw))
                        if step is not None:
                            steps.append(step)
            return cls(
                name=name,
                description=str(data.get("description", "")),
                format_filename=str(data.get("format_filename", "")),
                steps=steps,
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed serial definition entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "format_filename": self.format_filename,
            "steps": [step.to_dict() for step in self.steps],
        }
