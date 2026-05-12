"""Serial definition storage + validator + step renderer + executor.

Mirrors :mod:`services.composite` but for the new "Serial Requests" import
type. The Serial executor runs each step as an *independent* REST call (not
as part of a single ``/composite`` payload), evaluating a structured
predicate before each step and resolving ``@{ref.path}`` references
client-side from prior responses.

All four pieces stay GTK-free and synchronous; the page wraps execution in
a worker thread and translates progress events to the UI via
``GLib.idle_add`` (same discipline as the Composite page).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from platformdirs import PlatformDirs

from salesforce_object_flow.core.formats import FileFormat, slugify
from salesforce_object_flow.core.placeholders import (
    PLACEHOLDER_RE,
    REFERENCE_RE,
    render_csv_string,
    synthetic_row,
)
from salesforce_object_flow.core.serial import (
    METHODS_WITH_BODY,
    OPS_NUMERIC_VALUE,
    OPS_REQUIRING_PATH,
    OPS_REQUIRING_VALUE,
    REFERENCE_ID_RE,
    CheckOp,
    ConditionCheck,
    ConditionCombinator,
    HttpMethod,
    SerialDefinition,
    SerialStep,
    StepCondition,
)
from salesforce_object_flow.services.api import ApiError, SalesforceClient
from salesforce_object_flow.services.errors import CodedError, ErrorCode

log = logging.getLogger(__name__)

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=True)


class SerialDefinitionError(CodedError):
    """Save / Delete failures that must surface to the user."""


@dataclass(frozen=True, slots=True)
class LoadedDefinition:
    """A :class:`SerialDefinition` paired with its on-disk filename."""

    definition: SerialDefinition
    filename: str


# ====================================================================
# SerialDefinitionStore
# ====================================================================


def _default_root() -> Path:
    return Path(_DIRS.user_data_dir) / "serials"


class SerialDefinitionStore:
    """Disk-backed CRUD for :class:`SerialDefinition` JSON files."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _default_root()

    @property
    def root(self) -> Path:
        return self._root

    def list_definitions(self) -> list[LoadedDefinition]:
        if not self._root.exists():
            return []
        loaded: list[LoadedDefinition] = []
        for entry in self._root.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            payload = _safe_load_json(entry)
            if payload is None:
                continue
            definition = SerialDefinition.from_dict(payload)
            if definition is None:
                continue
            loaded.append(LoadedDefinition(definition=definition, filename=entry.name))
        loaded.sort(key=lambda ld: ld.definition.name.casefold())
        return loaded

    def load(self, filename: str) -> LoadedDefinition | None:
        path = self._root / filename
        payload = _safe_load_json(path)
        if payload is None:
            return None
        definition = SerialDefinition.from_dict(payload)
        if definition is None:
            return None
        return LoadedDefinition(definition=definition, filename=filename)

    def save(self, definition: SerialDefinition, *, previous_filename: str | None) -> str:
        target_filename = self._target_filename(definition, previous_filename=previous_filename)
        path = self._root / target_filename
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(definition.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            try:
                path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass
            raise SerialDefinitionError(
                f"Could not save serial definition: {exc}",
                code=ErrorCode.SERIAL_SAVE_FAILED,
                params={"error": str(exc)},
            ) from exc

        if previous_filename and previous_filename != target_filename:
            previous = self._root / previous_filename
            try:
                previous.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove old serial definition file %s: %s", previous, exc)

        return target_filename

    def delete(self, filename: str) -> bool:
        path = self._root / filename
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise SerialDefinitionError(
                f"Could not delete serial definition: {exc}",
                code=ErrorCode.SERIAL_DELETE_FAILED,
                params={"error": str(exc)},
            ) from exc
        return True

    def unique_filename_for(self, name: str, *, existing: Iterable[str]) -> str:
        base = slugify(name)
        existing_set = set(existing)
        candidate = f"{base}.json"
        counter = 2
        while candidate in existing_set:
            candidate = f"{base}-{counter}.json"
            counter += 1
        return candidate

    def _target_filename(
        self, definition: SerialDefinition, *, previous_filename: str | None
    ) -> str:
        slug = slugify(definition.name)
        candidate = f"{slug}.json"
        if previous_filename == candidate:
            return candidate

        existing: set[str] = set()
        if self._root.exists():
            existing = {
                entry.name
                for entry in self._root.iterdir()
                if entry.is_file() and entry.suffix == ".json"
            }
        if previous_filename is not None:
            existing.discard(previous_filename)

        if candidate not in existing:
            return candidate

        counter = 2
        while True:
            candidate = f"{slug}-{counter}.json"
            if candidate not in existing:
                return candidate
            counter += 1


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read serial definition file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Serial definition file %s is not a JSON object", path)
        return None
    return cast(dict[str, Any], data)


# ====================================================================
# SerialDefinitionValidator
# ====================================================================


@dataclass(frozen=True, slots=True)
class DefinitionError:
    """One validation finding. ``step_index == -1`` means definition-level.

    ``check_index >= 0`` pinpoints one entry inside a step's condition; ``-1``
    means the finding is on the step itself, not on one of its checks.
    """

    step_index: int
    field: str
    message: str
    check_index: int = -1


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[DefinitionError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


class SerialDefinitionValidator:
    """Static validation for a :class:`SerialDefinition` against its linked format."""

    def validate(self, definition: SerialDefinition, fmt: FileFormat | None) -> ValidationReport:
        errors: list[DefinitionError] = []

        if not definition.name.strip():
            errors.append(DefinitionError(-1, "name", "Name is required."))
        if not definition.format_filename.strip():
            errors.append(DefinitionError(-1, "format_filename", "Format link is required."))
        elif fmt is None:
            errors.append(
                DefinitionError(
                    -1,
                    "format_filename",
                    f'Linked format "{definition.format_filename}" not found.',
                )
            )
        if len(definition.steps) == 0:
            errors.append(DefinitionError(-1, "steps", "At least one step is required."))

        ref_counts = Counter(step.reference_id for step in definition.steps)
        duplicate_refs = {ref for ref, count in ref_counts.items() if count > 1 and ref}
        for index, step in enumerate(definition.steps):
            if step.reference_id in duplicate_refs:
                errors.append(
                    DefinitionError(
                        index,
                        "reference_id",
                        f'Duplicate reference id "{step.reference_id}".',
                    )
                )

        # Refs visible to step *i* are those of steps 0..i-1: no forward refs.
        refs_so_far: set[str] = set()
        for index, step in enumerate(definition.steps):
            errors.extend(self._check_step_shape(index, step))
            if fmt is not None:
                errors.extend(self._check_placeholders(index, step, fmt))
            errors.extend(self._check_references(index, step, refs_so_far))
            errors.extend(self._check_condition(index, step, refs_so_far))
            if step.reference_id:
                refs_so_far.add(step.reference_id)

        return ValidationReport(errors=tuple(errors))

    def _check_step_shape(self, index: int, step: SerialStep) -> list[DefinitionError]:
        errors: list[DefinitionError] = []
        if not step.reference_id:
            errors.append(DefinitionError(index, "reference_id", "Reference id is required."))
        elif not REFERENCE_ID_RE.match(step.reference_id):
            errors.append(
                DefinitionError(
                    index,
                    "reference_id",
                    f'Invalid reference id "{step.reference_id}" '
                    f"(must match [A-Za-z][A-Za-z0-9_]*).",
                )
            )
        if not step.url:
            errors.append(DefinitionError(index, "url", "URL is required."))
        elif not step.url.startswith("/"):
            errors.append(DefinitionError(index, "url", 'URL must start with "/".'))
        if step.body:
            if step.method not in METHODS_WITH_BODY:
                errors.append(
                    DefinitionError(
                        index,
                        "body",
                        f"Body not allowed for {step.method.value}.",
                    )
                )
            seen_fields: set[str] = set()
            for entry in step.body:
                name = entry.field.strip()
                if not name:
                    errors.append(DefinitionError(index, "body", "Body field name is required."))
                    continue
                if name in seen_fields:
                    errors.append(
                        DefinitionError(
                            index,
                            "body",
                            f'Duplicate body field "{name}".',
                        )
                    )
                seen_fields.add(name)
        return errors

    def _check_placeholders(
        self, index: int, step: SerialStep, fmt: FileFormat
    ) -> list[DefinitionError]:
        column_names = {col.name for col in fmt.columns}
        errors: list[DefinitionError] = []
        seen_unknown: set[str] = set()

        def check_string(text: str, field_name: str) -> None:
            for match in PLACEHOLDER_RE.finditer(text):
                token = match.group(1).strip()
                if token in column_names:
                    continue
                if token in seen_unknown:
                    continue
                seen_unknown.add(token)
                errors.append(
                    DefinitionError(
                        index,
                        field_name,
                        f'Unknown column "{token}" referenced as ' + "{{" + token + "}}.",
                    )
                )

        check_string(step.url, "url")
        if step.body:
            for entry in step.body:
                check_string(entry.value, "body")
        for header_value in step.headers.values():
            check_string(header_value, "headers")

        return errors

    def _check_references(
        self, index: int, step: SerialStep, valid_refs: set[str]
    ) -> list[DefinitionError]:
        errors: list[DefinitionError] = []
        seen_unknown: set[str] = set()

        def check_string(text: str, field_name: str) -> None:
            for match in REFERENCE_RE.finditer(text):
                ref = match.group(1)
                if ref in valid_refs:
                    continue
                if ref in seen_unknown:
                    continue
                seen_unknown.add(ref)
                errors.append(
                    DefinitionError(
                        index,
                        field_name,
                        f'Reference "@{{{ref}.…}}" does not point to a previous step.',
                    )
                )

        check_string(step.url, "url")
        if step.body:
            for entry in step.body:
                check_string(entry.value, "body")
        for header_value in step.headers.values():
            check_string(header_value, "headers")

        return errors

    def _check_condition(
        self, index: int, step: SerialStep, valid_refs: set[str]
    ) -> list[DefinitionError]:
        condition = step.condition
        if condition is None:
            return []
        errors: list[DefinitionError] = []
        for check_index, check in enumerate(condition.checks):
            if not check.ref:
                errors.append(
                    DefinitionError(
                        index,
                        "condition.ref",
                        "Condition check is missing a reference.",
                        check_index=check_index,
                    )
                )
            elif check.ref not in valid_refs:
                errors.append(
                    DefinitionError(
                        index,
                        "condition.ref",
                        f'Condition references "{check.ref}", which is not a previous step.',
                        check_index=check_index,
                    )
                )
            if check.op in OPS_REQUIRING_PATH and not check.path.strip():
                errors.append(
                    DefinitionError(
                        index,
                        "condition.path",
                        f"Op {check.op.value} requires a path.",
                        check_index=check_index,
                    )
                )
            if check.op in OPS_REQUIRING_VALUE and not check.value.strip():
                errors.append(
                    DefinitionError(
                        index,
                        "condition.value",
                        f"Op {check.op.value} requires a value.",
                        check_index=check_index,
                    )
                )
            if check.op in OPS_NUMERIC_VALUE:
                try:
                    int(check.value)
                except (TypeError, ValueError):
                    errors.append(
                        DefinitionError(
                            index,
                            "condition.value",
                            f"Op {check.op.value} requires an integer value.",
                            check_index=check_index,
                        )
                    )
        return errors


# ====================================================================
# Reference path resolution + condition evaluation
# ====================================================================


_PATH_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def navigate_path(root: object, path: str) -> object | None:
    """Navigate a dotted/indexed path inside a JSON-ish structure.

    Examples: ``"id"``, ``"records[0].Id"``, ``"results[2].body.id"``.
    Returns ``None`` if any segment misses (rather than raising).
    """
    if not path:
        return root
    current: object | None = root
    for match in _PATH_TOKEN_RE.finditer(path):
        if current is None:
            return None
        key, index = match.group(1), match.group(2)
        if index is not None:
            if not isinstance(current, list):
                return None
            i = int(index)
            current_list = cast(list[Any], current)
            if i < 0 or i >= len(current_list):
                return None
            current = current_list[i]
        else:
            if not isinstance(current, dict):
                return None
            current = cast(dict[str, Any], current).get(key)
    return current


@dataclass(frozen=True, slots=True)
class StepResult:
    """Outcome of one :class:`SerialStep` for one CSV row.

    ``status`` is ``"success"`` (HTTP 2xx), ``"failure"`` (everything else,
    including render and HTTP errors), or ``"skipped"`` (the condition did
    not hold). ``body`` is the parsed JSON response on success, an error
    payload on failure, or ``None`` on skipped.
    """

    reference_id: str
    status: Literal["success", "failure", "skipped"]
    http_status: int
    body: object
    error_summary: str | None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def evaluate_condition(condition: StepCondition | None, prior: Mapping[str, StepResult]) -> bool:
    """Return whether ``condition`` holds against the results gathered so far.

    A missing or empty condition always returns ``True``.
    """
    if condition is None or not condition.checks:
        return True
    if condition.combinator is ConditionCombinator.ALL_OF:
        return all(evaluate_check(check, prior) for check in condition.checks)
    return any(evaluate_check(check, prior) for check in condition.checks)


def evaluate_check(check: ConditionCheck, prior: Mapping[str, StepResult]) -> bool:
    """Evaluate one :class:`ConditionCheck` against prior step results."""
    result = prior.get(check.ref)
    if result is None:
        return False
    if check.op is CheckOp.STATUS_OK:
        return result.ok
    if check.op is CheckOp.STATUS_FAILED:
        return result.status == "failure"
    if not result.ok:
        # body-shape predicates require a real successful response.
        return False
    value = navigate_path(result.body, check.path)
    match check.op:
        case CheckOp.EXISTS:
            return value not in (None, "", [], {})
        case CheckOp.NOT_EXISTS:
            return value in (None, "", [], {})
        case CheckOp.EQ:
            return value is not None and str(value) == check.value
        case CheckOp.NE:
            return value is not None and str(value) != check.value
        case CheckOp.RECORDS_COUNT_EQ:
            try:
                target = int(check.value)
            except (TypeError, ValueError):
                return False
            return isinstance(value, list) and len(value) == target
        case CheckOp.RECORDS_COUNT_GT:
            try:
                target = int(check.value)
            except (TypeError, ValueError):
                return False
            return isinstance(value, list) and len(value) > target
        case _:
            return False


# ====================================================================
# SerialStepRenderer
# ====================================================================


@dataclass(frozen=True, slots=True)
class RenderRow:
    """One CSV-shaped sample row keyed by column name (string values)."""

    values: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RenderedRequest:
    """Output of :meth:`SerialStepRenderer.render_step`: ready to dispatch."""

    method: HttpMethod
    url: str
    body: dict[str, object] | None
    headers: dict[str, str]


class StepRenderError(CodedError):
    """Raised when a reference (``@{ref.path}``) cannot be resolved at render time."""


class SerialStepRenderer:
    """Render one :class:`SerialStep` to a :class:`RenderedRequest`.

    Substitutes ``{{col}}`` placeholders against the CSV row and resolves
    ``@{ref.path}`` against prior step results. Both kinds of tokens can
    appear in URL, headers, and body values.
    """

    def render_step(
        self,
        step: SerialStep,
        fmt: FileFormat,
        row: RenderRow,
        prior: Mapping[str, StepResult],
    ) -> RenderedRequest:
        column_by_name = {col.name: col for col in fmt.columns}

        def render_value(text: str) -> object:
            resolved = _resolve_references(text, prior)
            csv_input = resolved if isinstance(resolved, str) else str(resolved)
            return render_csv_string(csv_input, column_by_name, row.values)

        def render_str(text: str) -> str:
            value = render_value(text)
            return value if isinstance(value, str) else str(value)

        rendered_url = render_str(step.url)
        rendered_body: dict[str, object] | None = None
        if step.body:
            rendered_body = {}
            for entry in step.body:
                name = entry.field.strip()
                if not name:
                    continue
                rendered_body[name] = render_value(entry.value)
        rendered_headers = {
            key: render_str(value) for key, value in step.headers.items()
        } if step.headers else {}

        return RenderedRequest(
            method=step.method,
            url=rendered_url,
            body=rendered_body,
            headers=rendered_headers,
        )

    @staticmethod
    def synthetic_row(fmt: FileFormat) -> RenderRow:
        return RenderRow(values=synthetic_row(fmt))


def _resolve_references(text: str, prior: Mapping[str, StepResult]) -> str:
    """Replace every ``@{ref.path}`` token in *text* with its resolved value.

    Unresolved references become an empty string — the executor surfaces
    Salesforce's own error if that produces a malformed request, which is
    much more informative than us inventing a synthetic one.
    """

    def repl(match: re.Match[str]) -> str:
        inner = match.group(0)[2:-1].strip()  # strip the surrounding @{ }
        head, dot, tail = inner.partition(".")
        ref = head.strip()
        path = tail.strip()
        result = prior.get(ref)
        if result is None:
            return ""
        if path == "status":
            return str(result.http_status)
        if path == "ok":
            return "true" if result.ok else "false"
        if path == "skipped":
            return "true" if result.skipped else "false"
        if not result.ok:
            return ""
        value = navigate_path(result.body, path)
        if value is None:
            return ""
        return str(value)

    return REFERENCE_RE.sub(repl, text)


# ====================================================================
# Executor
# ====================================================================


@dataclass(frozen=True, slots=True)
class RowResult:
    """Outcome of running a definition against one CSV row."""

    row_index: int
    csv_row: Mapping[str, str]
    status: Literal["success", "failure", "cancelled"]
    step_results: tuple[StepResult, ...]
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    total: int
    succeeded: int
    failed: int
    cancelled: bool
    rows: tuple[RowResult, ...]

    @property
    def has_failures(self) -> bool:
        return self.failed > 0


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    processed: int
    total: int
    last_result: RowResult | None


class ExecutionError(CodedError):
    """Fatal: aborts the whole run. The message is safe to show to the user."""


class SerialExecutor:
    """Run a :class:`SerialDefinition` once per CSV row, in series.

    Same threading discipline as :class:`services.composite.CompositeExecutor`:
    GTK-free, synchronous, cancellation via a caller-owned
    :class:`threading.Event`.
    """

    def __init__(self, renderer: SerialStepRenderer | None = None) -> None:
        self._renderer = renderer if renderer is not None else SerialStepRenderer()

    def run(
        self,
        definition: SerialDefinition,
        fmt: FileFormat,
        csv_path: Path,
        sf_client: SalesforceClient,
        *,
        on_progress: Callable[[ProgressEvent], None],
        cancelled: threading.Event,
    ) -> ExecutionReport:
        rows = self._read_all_rows(fmt, csv_path)
        total = len(rows)
        column_names = [col.name for col in fmt.columns]

        results: list[RowResult] = []
        succeeded = 0
        failed = 0
        was_cancelled = False

        for row_index, raw_cells in enumerate(rows):
            if cancelled.is_set():
                was_cancelled = True
                break

            csv_row: dict[str, str] = {}
            mismatch: str | None = None
            if len(raw_cells) != len(column_names):
                mismatch = (
                    f"Column count mismatch: expected {len(column_names)}, got {len(raw_cells)}."
                )
                for i, name in enumerate(column_names):
                    csv_row[name] = raw_cells[i] if i < len(raw_cells) else ""
            else:
                for name, value in zip(column_names, raw_cells, strict=True):
                    csv_row[name] = value

            if mismatch is not None:
                row_result = RowResult(
                    row_index=row_index,
                    csv_row=csv_row,
                    status="failure",
                    step_results=(),
                    error_summary=mismatch,
                )
                failed += 1
                results.append(row_result)
                on_progress(ProgressEvent(row_index + 1, total, row_result))
                continue

            row_result = self._run_row(
                row_index, csv_row, definition, fmt, sf_client
            )
            if row_result.status == "success":
                succeeded += 1
            else:
                failed += 1
            results.append(row_result)
            on_progress(ProgressEvent(row_index + 1, total, row_result))

        return ExecutionReport(
            total=total,
            succeeded=succeeded,
            failed=failed,
            cancelled=was_cancelled,
            rows=tuple(results),
        )

    def _run_row(
        self,
        row_index: int,
        csv_row: Mapping[str, str],
        definition: SerialDefinition,
        fmt: FileFormat,
        sf_client: SalesforceClient,
    ) -> RowResult:
        prior: dict[str, StepResult] = {}
        step_results: list[StepResult] = []
        render_row = RenderRow(values=dict(csv_row))
        first_failure: str | None = None
        aborted = False

        for step in definition.steps:
            if not evaluate_condition(step.condition, prior):
                skipped = StepResult(
                    reference_id=step.reference_id,
                    status="skipped",
                    http_status=0,
                    body=None,
                    error_summary=None,
                )
                step_results.append(skipped)
                prior[step.reference_id] = skipped
                continue

            try:
                rendered = self._renderer.render_step(step, fmt, render_row, prior)
            except StepRenderError as exc:
                result = StepResult(
                    reference_id=step.reference_id,
                    status="failure",
                    http_status=0,
                    body=None,
                    error_summary=f"Render error: {exc}",
                )
                step_results.append(result)
                prior[step.reference_id] = result
                if first_failure is None:
                    first_failure = f"{step.reference_id}: {result.error_summary}"
                if not step.continue_on_failure:
                    aborted = True
                    break
                continue
            except Exception as exc:  # defensive: any unexpected render bug
                log.exception(
                    "Unexpected render failure on row %d step %s",
                    row_index,
                    step.reference_id,
                )
                result = StepResult(
                    reference_id=step.reference_id,
                    status="failure",
                    http_status=0,
                    body=None,
                    error_summary=f"Render error: {exc}",
                )
                step_results.append(result)
                prior[step.reference_id] = result
                if first_failure is None:
                    first_failure = f"{step.reference_id}: {result.error_summary}"
                if not step.continue_on_failure:
                    aborted = True
                    break
                continue

            try:
                response = sf_client.request(
                    rendered.method.value,
                    rendered.url,
                    json=rendered.body,
                )
                http_status = 200
                result = StepResult(
                    reference_id=step.reference_id,
                    status="success",
                    http_status=http_status,
                    body=response,
                    error_summary=None,
                )
            except ApiError as exc:
                if exc.status_code == 401:
                    raise ExecutionError(
                        "Authentication failed during execution; re-authenticate and retry.",
                        code=ErrorCode.AUTH_FAILED,
                    ) from exc
                summary = (
                    f"HTTP {exc.status_code}: {exc}"
                    if exc.status_code is not None
                    else f"Network error: {exc}"
                )
                result = StepResult(
                    reference_id=step.reference_id,
                    status="failure",
                    http_status=exc.status_code or 0,
                    body=None,
                    error_summary=summary,
                )

            step_results.append(result)
            prior[step.reference_id] = result

            if result.status != "success":
                if first_failure is None:
                    first_failure = f"{step.reference_id}: {result.error_summary}"
                if not step.continue_on_failure:
                    aborted = True
                    break

        # Row status: success only if every executed step succeeded.
        executed = [r for r in step_results if r.status != "skipped"]
        all_ok = bool(executed) and all(r.ok for r in executed) and not aborted
        return RowResult(
            row_index=row_index,
            csv_row=csv_row,
            status="success" if all_ok else "failure",
            step_results=tuple(step_results),
            error_summary=None if all_ok else (first_failure or "No step executed."),
        )

    @staticmethod
    def _read_all_rows(fmt: FileFormat, csv_path: Path) -> list[list[str]]:
        try:
            text = csv_path.read_text(encoding=fmt.encoding)
        except OSError as exc:
            raise ExecutionError(
                f"CSV unreadable: {exc}",
                code=ErrorCode.CSV_UNREADABLE,
                params={"error": str(exc)},
            ) from exc
        except UnicodeDecodeError as exc:
            raise ExecutionError(
                f"Could not decode {csv_path} as {fmt.encoding}: {exc}",
                code=ErrorCode.CSV_DECODE_ERROR,
                params={"path": str(csv_path), "encoding": fmt.encoding, "error": str(exc)},
            ) from exc

        reader = csv.reader(
            text.splitlines(),
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
        )
        rows = [list(row) for row in reader]
        if fmt.has_header and rows:
            rows = rows[1:]
        return rows


def export_failures_csv(
    report: ExecutionReport,
    fmt: FileFormat,
    output_path: Path,
) -> int:
    """Write the failed rows of *report* to *output_path* with an ``_error`` column."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    column_names = [col.name for col in fmt.columns]
    header = [*column_names, "_error"]
    written = 0
    with output_path.open("w", encoding=fmt.encoding, newline="") as handle:
        writer = csv.writer(
            handle,
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writerow(header)
        for row in report.rows:
            if row.status != "failure":
                continue
            line = [row.csv_row.get(name, "") for name in column_names]
            line.append(row.error_summary or "")
            writer.writerow(line)
            written += 1
    return written
