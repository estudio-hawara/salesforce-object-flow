"""Composite template storage + validator + payload renderer + executor.

:class:`CompositeTemplateStore` does CRUD on the user's
``user_data_dir / templates/`` directory: one JSON file per template,
slugified filenames, atomic writes. Same on-disk discipline as
:mod:`services.formats`: corruption is tolerated on read (logged and
skipped), but write failures surface as :class:`CompositeTemplateError`.

:class:`CompositeTemplateValidator` runs a :class:`CompositeTemplate`
against its linked :class:`FileFormat` and yields per-subrequest errors
(unknown ``{{col}}`` placeholders, dangling ``@{ref.path}`` references,
shape problems, etc.). It does *not* hit the network.

:class:`CompositePayloadRenderer` produces the final JSON payload that
would be POSTed to ``/services/data/<v>/composite``: it walks the
template, substitutes typed placeholders against a :class:`RenderRow`,
and leaves ``@{ref.path}`` literals untouched (Salesforce resolves
those server-side at execution time).

:class:`CompositeExecutor` reads a CSV row by row, renders one Composite
payload per row, posts it through a caller-supplied
:class:`SalesforceClient`, classifies the response and emits progress
events. It is GTK-free and synchronous; the caller drives the worker
thread + ``GLib.idle_add`` translation.

All four stay GTK-free and synchronous.
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

from salesforce_object_flow.core.composite import (
    MAX_SUBREQUESTS,
    METHODS_WITH_BODY,
    REFERENCE_ID_RE,
    CompositeTemplate,
    Subrequest,
)
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat, slugify
from salesforce_object_flow.services.api import ApiError, SalesforceClient

log = logging.getLogger(__name__)

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=True)

PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
REFERENCE_RE: Final[re.Pattern[str]] = re.compile(r"@\{\s*([A-Za-z][A-Za-z0-9_]*)\.[^}]+?\s*\}")
_PLACEHOLDER_ONLY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\{\{\s*([^{}]+?)\s*\}\}\s*$")
_BOOLEAN_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_BOOLEAN_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no"})


class CompositeTemplateError(RuntimeError):
    """Save / Delete failures that must surface to the user."""


@dataclass(frozen=True, slots=True)
class LoadedTemplate:
    """A :class:`CompositeTemplate` paired with its on-disk filename."""

    template: CompositeTemplate
    filename: str


# ====================================================================
# CompositeTemplateStore
# ====================================================================


def _default_root() -> Path:
    return Path(_DIRS.user_data_dir) / "templates"


class CompositeTemplateStore:
    """Disk-backed CRUD for :class:`CompositeTemplate` JSON files."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _default_root()

    @property
    def root(self) -> Path:
        return self._root

    def list_templates(self) -> list[LoadedTemplate]:
        """Return every template on disk, sorted by name. Malformed files skipped."""
        if not self._root.exists():
            return []
        loaded: list[LoadedTemplate] = []
        for entry in self._root.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            payload = _safe_load_json(entry)
            if payload is None:
                continue
            tpl = CompositeTemplate.from_dict(payload)
            if tpl is None:
                continue
            loaded.append(LoadedTemplate(template=tpl, filename=entry.name))
        loaded.sort(key=lambda lt: lt.template.name.casefold())
        return loaded

    def load(self, filename: str) -> LoadedTemplate | None:
        path = self._root / filename
        payload = _safe_load_json(path)
        if payload is None:
            return None
        tpl = CompositeTemplate.from_dict(payload)
        if tpl is None:
            return None
        return LoadedTemplate(template=tpl, filename=filename)

    def save(self, tpl: CompositeTemplate, *, previous_filename: str | None) -> str:
        """Persist *tpl* to disk and return the resulting filename.

        If *previous_filename* differs from the new slug-derived filename, the
        old file is removed in a best-effort pass after the new one lands.
        """
        target_filename = self._target_filename(tpl, previous_filename=previous_filename)
        path = self._root / target_filename
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(tpl.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            try:
                path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass
            raise CompositeTemplateError(f"Could not save template: {exc}") from exc

        if previous_filename and previous_filename != target_filename:
            previous = self._root / previous_filename
            try:
                previous.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove old template file %s: %s", previous, exc)

        return target_filename

    def delete(self, filename: str) -> bool:
        path = self._root / filename
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise CompositeTemplateError(f"Could not delete template: {exc}") from exc
        return True

    def unique_filename_for(self, name: str, *, existing: Iterable[str]) -> str:
        """Return an unused ``<slug>.json`` (or ``<slug>-N.json``)."""
        base = slugify(name)
        existing_set = set(existing)
        candidate = f"{base}.json"
        counter = 2
        while candidate in existing_set:
            candidate = f"{base}-{counter}.json"
            counter += 1
        return candidate

    def _target_filename(self, tpl: CompositeTemplate, *, previous_filename: str | None) -> str:
        slug = slugify(tpl.name)
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
        log.warning("Could not read template file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Template file %s is not a JSON object", path)
        return None
    return cast(dict[str, Any], data)


# ====================================================================
# CompositeTemplateValidator
# ====================================================================


@dataclass(frozen=True, slots=True)
class TemplateError:
    """One validation finding. ``subrequest_index == -1`` means template-level."""

    subrequest_index: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[TemplateError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


class CompositeTemplateValidator:
    """Static validation for a :class:`CompositeTemplate` against its linked format."""

    def validate(self, tpl: CompositeTemplate, fmt: FileFormat | None) -> ValidationReport:
        errors: list[TemplateError] = []

        # ---- Template-level shape ---------------------------------------
        if not tpl.name.strip():
            errors.append(TemplateError(-1, "name", "Name is required."))
        if not tpl.format_filename.strip():
            errors.append(TemplateError(-1, "format_filename", "Format link is required."))
        elif fmt is None:
            errors.append(
                TemplateError(
                    -1,
                    "format_filename",
                    f'Linked format "{tpl.format_filename}" not found.',
                )
            )
        if len(tpl.subrequests) == 0:
            errors.append(TemplateError(-1, "subrequests", "At least one subrequest is required."))
        if len(tpl.subrequests) > MAX_SUBREQUESTS:
            errors.append(
                TemplateError(
                    -1,
                    "subrequests",
                    f"Maximum {MAX_SUBREQUESTS} subrequests; got {len(tpl.subrequests)}.",
                )
            )

        # ---- Reference-id uniqueness across subrequests -----------------
        ref_counts = Counter(sub.reference_id for sub in tpl.subrequests)
        duplicate_refs = {ref for ref, count in ref_counts.items() if count > 1 and ref}
        for index, sub in enumerate(tpl.subrequests):
            if sub.reference_id in duplicate_refs:
                errors.append(
                    TemplateError(
                        index,
                        "reference_id",
                        f'Duplicate reference id "{sub.reference_id}".',
                    )
                )

        valid_refs = {sub.reference_id for sub in tpl.subrequests if sub.reference_id}

        # ---- Per-subrequest passes --------------------------------------
        for index, sub in enumerate(tpl.subrequests):
            errors.extend(self._check_subrequest_shape(index, sub))
            if fmt is not None:
                errors.extend(self._check_placeholders(index, sub, fmt))
            errors.extend(self._check_references(index, sub, valid_refs))

        return ValidationReport(errors=tuple(errors))

    def _check_subrequest_shape(self, index: int, sub: Subrequest) -> list[TemplateError]:
        errors: list[TemplateError] = []
        if not sub.reference_id:
            errors.append(TemplateError(index, "reference_id", "Reference id is required."))
        elif not REFERENCE_ID_RE.match(sub.reference_id):
            errors.append(
                TemplateError(
                    index,
                    "reference_id",
                    f'Invalid reference id "{sub.reference_id}" '
                    f"(must match [A-Za-z][A-Za-z0-9_]*).",
                )
            )
        if not sub.url:
            errors.append(TemplateError(index, "url", "URL is required."))
        elif not sub.url.startswith("/"):
            errors.append(TemplateError(index, "url", 'URL must start with "/".'))
        if sub.body:
            if sub.method not in METHODS_WITH_BODY:
                errors.append(
                    TemplateError(
                        index,
                        "body",
                        f"Body not allowed for {sub.method.value}.",
                    )
                )
            seen_fields: set[str] = set()
            for entry in sub.body:
                name = entry.field.strip()
                if not name:
                    errors.append(TemplateError(index, "body", "Body field name is required."))
                    continue
                if name in seen_fields:
                    errors.append(
                        TemplateError(
                            index,
                            "body",
                            f'Duplicate body field "{name}".',
                        )
                    )
                seen_fields.add(name)
        return errors

    def _check_placeholders(
        self, index: int, sub: Subrequest, fmt: FileFormat
    ) -> list[TemplateError]:
        column_names = {col.name for col in fmt.columns}
        errors: list[TemplateError] = []

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
                    TemplateError(
                        index,
                        field_name,
                        f'Unknown column "{token}" referenced as ' + "{{" + token + "}}.",
                    )
                )

        check_string(sub.url, "url")
        if sub.body:
            for entry in sub.body:
                check_string(entry.value, "body")
        for header_value in sub.headers.values():
            check_string(header_value, "headers")

        return errors

    def _check_references(
        self, index: int, sub: Subrequest, valid_refs: set[str]
    ) -> list[TemplateError]:
        errors: list[TemplateError] = []
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
                    TemplateError(
                        index,
                        field_name,
                        f'Reference "@{{{ref}.…}}" does not match any subrequest\'s reference_id.',
                    )
                )

        check_string(sub.url, "url")
        if sub.body:
            for entry in sub.body:
                check_string(entry.value, "body")
        for header_value in sub.headers.values():
            check_string(header_value, "headers")

        return errors


# ====================================================================
# CompositePayloadRenderer
# ====================================================================


@dataclass(frozen=True, slots=True)
class RenderRow:
    """One CSV-shaped sample row keyed by column name (string values)."""

    values: Mapping[str, str]


class CompositePayloadRenderer:
    """Render a :class:`CompositeTemplate` to a Composite-API JSON payload."""

    def render(self, tpl: CompositeTemplate, fmt: FileFormat, row: RenderRow) -> dict[str, Any]:
        column_by_name = {col.name: col for col in fmt.columns}
        composite_request: list[dict[str, Any]] = []
        for sub in tpl.subrequests:
            entry: dict[str, Any] = {
                "method": sub.method.value,
                "url": _render_string(sub.url, column_by_name, row),
                "referenceId": sub.reference_id,
            }
            if sub.body:
                body_obj: dict[str, object] = {}
                for body_entry in sub.body:
                    name = body_entry.field.strip()
                    if not name:
                        continue
                    body_obj[name] = _render_string(body_entry.value, column_by_name, row)
                entry["body"] = body_obj
            if sub.headers:
                entry["httpHeaders"] = {
                    key: _render_string(value, column_by_name, row)
                    for key, value in sub.headers.items()
                }
            composite_request.append(entry)
        return {
            "allOrNone": tpl.all_or_none,
            "collateSubrequests": tpl.collate_subrequests,
            "compositeRequest": composite_request,
        }

    @staticmethod
    def synthetic_row(fmt: FileFormat) -> RenderRow:
        values: dict[str, str] = {}
        for col in fmt.columns:
            values[col.name] = _synthetic_value(col)
        return RenderRow(values=values)


def _synthetic_value(column: Column) -> str:
    match column.type:
        case ColumnType.STRING:
            return "<string>"
        case ColumnType.INTEGER:
            return "0"
        case ColumnType.DECIMAL:
            return "0.0"
        case ColumnType.BOOLEAN:
            return "false"
        case ColumnType.DATE:
            return "2026-01-01"
        case ColumnType.DATETIME:
            return "2026-01-01T00:00:00"
        case ColumnType.EMAIL:
            return "user@example.com"


def _render_string(value: str, column_by_name: Mapping[str, Column], row: RenderRow) -> object:
    only = _PLACEHOLDER_ONLY_RE.match(value)
    if only is not None:
        token = only.group(1).strip()
        column = column_by_name.get(token)
        if column is None:
            return value
        raw = row.values.get(token, "")
        return _coerce(raw, column)
    # Mixed-content string: every {{col}} becomes its raw cell value (string).
    return PLACEHOLDER_RE.sub(lambda m: row.values.get(m.group(1).strip(), m.group(0)), value)


def _coerce(raw: str, column: Column) -> object:
    try:
        match column.type:
            case ColumnType.STRING | ColumnType.EMAIL | ColumnType.DATE | ColumnType.DATETIME:
                return raw
            case ColumnType.INTEGER:
                return int(raw)
            case ColumnType.DECIMAL:
                return float(raw)
            case ColumnType.BOOLEAN:
                folded = raw.casefold()
                if folded in _BOOLEAN_TRUE:
                    return True
                if folded in _BOOLEAN_FALSE:
                    return False
                return raw
    except (ValueError, TypeError):
        return raw


# ====================================================================
# CompositeExecutor
# ====================================================================


@dataclass(frozen=True, slots=True)
class SalesforceError:
    """One per-subrequest error returned by Salesforce in a Composite reply."""

    error_code: str
    message: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubrequestResult:
    """Outcome of a single subrequest within one Composite call."""

    reference_id: str
    http_status: int
    body: object
    errors: tuple[SalesforceError, ...]

    @property
    def ok(self) -> bool:
        return 200 <= self.http_status < 300


@dataclass(frozen=True, slots=True)
class RowResult:
    """Outcome of running the template against one CSV row."""

    row_index: int
    csv_row: Mapping[str, str]
    status: Literal["success", "failure", "cancelled"]
    subrequest_results: tuple[SubrequestResult, ...]
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Outcome of a full execution run."""

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
    """A progress notification from :class:`CompositeExecutor.run`."""

    processed: int
    total: int
    last_result: RowResult | None


class ExecutionError(RuntimeError):
    """Fatal: aborts the whole run. The message is safe to show to the user."""


_PROCESSING_HALTED: Final[str] = "PROCESSING_HALTED"


class CompositeExecutor:
    """Run a :class:`CompositeTemplate` once per CSV row.

    The executor is GTK-free and synchronous. The caller is expected to invoke
    :meth:`run` from a worker thread and translate ``on_progress`` calls back
    to the UI thread (e.g. with ``GLib.idle_add``). Cancellation is
    cooperative: the caller flips ``cancelled`` and the executor returns at
    the next iteration boundary, never interrupting an in-flight POST.
    """

    def __init__(self, renderer: CompositePayloadRenderer | None = None) -> None:
        self._renderer = renderer if renderer is not None else CompositePayloadRenderer()

    def run(
        self,
        tpl: CompositeTemplate,
        fmt: FileFormat,
        csv_path: Path,
        sf_client: SalesforceClient,
        *,
        on_progress: Callable[[ProgressEvent], None],
        cancelled: threading.Event,
    ) -> ExecutionReport:
        rows = self._read_all_rows(fmt, csv_path)
        total = len(rows)
        path = f"/services/data/{sf_client.api_version}/composite"
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
                # Keep whatever we have so the export can still emit the row.
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
                    subrequest_results=(),
                    error_summary=mismatch,
                )
                failed += 1
                results.append(row_result)
                on_progress(ProgressEvent(row_index + 1, total, row_result))
                continue

            try:
                payload = self._renderer.render(tpl, fmt, RenderRow(values=dict(csv_row)))
            except Exception as exc:
                log.exception("Render failure on row %d", row_index)
                row_result = RowResult(
                    row_index=row_index,
                    csv_row=csv_row,
                    status="failure",
                    subrequest_results=(),
                    error_summary=f"Render error: {exc}",
                )
                failed += 1
                results.append(row_result)
                on_progress(ProgressEvent(row_index + 1, total, row_result))
                continue

            try:
                response = sf_client.post(path, payload)
            except ApiError as exc:
                if exc.status_code == 401:
                    raise ExecutionError(
                        "Authentication failed during execution; re-authenticate and retry."
                    ) from exc
                summary = (
                    f"HTTP {exc.status_code}: {exc}"
                    if exc.status_code is not None
                    else f"Network error: {exc}"
                )
                row_result = RowResult(
                    row_index=row_index,
                    csv_row=csv_row,
                    status="failure",
                    subrequest_results=(),
                    error_summary=summary,
                )
                failed += 1
                results.append(row_result)
                on_progress(ProgressEvent(row_index + 1, total, row_result))
                continue

            sub_results = self._classify_response(response)
            row_result = self._build_row_result(row_index, csv_row, sub_results)
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

    @staticmethod
    def _read_all_rows(fmt: FileFormat, csv_path: Path) -> list[list[str]]:
        try:
            text = csv_path.read_text(encoding=fmt.encoding)
        except OSError as exc:
            raise ExecutionError(f"CSV unreadable: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ExecutionError(f"Could not decode {csv_path} as {fmt.encoding}: {exc}") from exc

        reader = csv.reader(
            text.splitlines(),
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
        )
        rows = [list(row) for row in reader]
        if fmt.has_header and rows:
            rows = rows[1:]
        return rows

    @staticmethod
    def _classify_response(response: object) -> tuple[SubrequestResult, ...]:
        if not isinstance(response, dict):
            return ()
        envelope = cast(dict[str, Any], response)
        composite = envelope.get("compositeResponse", [])
        if not isinstance(composite, list):
            return ()
        sub_results: list[SubrequestResult] = []
        for entry in cast(list[Any], composite):
            if not isinstance(entry, dict):
                continue
            entry_d = cast(dict[str, Any], entry)
            ref = str(entry_d.get("referenceId", ""))
            status_raw = entry_d.get("httpStatusCode", 0)
            try:
                status = int(status_raw)
            except (TypeError, ValueError):
                status = 0
            body = entry_d.get("body", None)
            errors: tuple[SalesforceError, ...] = ()
            if status >= 400:
                errors = _extract_errors(body)
            sub_results.append(
                SubrequestResult(
                    reference_id=ref,
                    http_status=status,
                    body=body,
                    errors=errors,
                )
            )
        return tuple(sub_results)

    @staticmethod
    def _build_row_result(
        row_index: int,
        csv_row: Mapping[str, str],
        sub_results: tuple[SubrequestResult, ...],
    ) -> RowResult:
        if all(sub.ok for sub in sub_results) and sub_results:
            return RowResult(
                row_index=row_index,
                csv_row=csv_row,
                status="success",
                subrequest_results=sub_results,
                error_summary=None,
            )
        # Failure path. Pick the first failed subrequest and the first
        # non-PROCESSING_HALTED error within the whole row as the summary.
        failed = [sub for sub in sub_results if not sub.ok]
        first_failed = failed[0] if failed else None
        real_errors = [
            err for sub in failed for err in sub.errors if err.error_code != _PROCESSING_HALTED
        ]
        fallback = next((err for sub in failed for err in sub.errors), None)
        chosen = real_errors[0] if real_errors else fallback
        if first_failed is None:
            # No subrequests at all in the response; treat as failure.
            summary = "Empty compositeResponse from Salesforce."
        elif chosen is not None:
            summary = f"{first_failed.reference_id}: {chosen.error_code}: {chosen.message}"
        else:
            summary = f"{first_failed.reference_id}: HTTP {first_failed.http_status}"
        return RowResult(
            row_index=row_index,
            csv_row=csv_row,
            status="failure",
            subrequest_results=sub_results,
            error_summary=summary,
        )


def _extract_errors(body: object) -> tuple[SalesforceError, ...]:
    if isinstance(body, list):
        items: list[SalesforceError] = []
        body_list = cast(list[Any], body)
        for entry in body_list:
            if isinstance(entry, dict):
                items.append(_one_error(cast(dict[str, Any], entry)))
        if items:
            return tuple(items)
        return (SalesforceError(error_code="UNKNOWN", message=repr(body_list), fields=()),)
    if isinstance(body, dict):
        return (_one_error(cast(dict[str, Any], body)),)
    return (SalesforceError(error_code="UNKNOWN", message=str(body), fields=()),)


def _one_error(entry: Mapping[str, Any]) -> SalesforceError:
    code = str(entry.get("errorCode") or entry.get("error") or "UNKNOWN")
    message = str(entry.get("message") or entry.get("error_description") or "")
    raw_fields = entry.get("fields", [])
    fields: tuple[str, ...] = ()
    if isinstance(raw_fields, list):
        fields = tuple(str(item) for item in cast(list[Any], raw_fields))
    return SalesforceError(error_code=code, message=message, fields=fields)


def export_failures_csv(
    report: ExecutionReport,
    fmt: FileFormat,
    output_path: Path,
) -> int:
    """Write the failed rows of *report* to *output_path*.

    Header is the FileFormat columns plus an ``_error`` column. Rows with
    ``status == "cancelled"`` are excluded (they never made it to the server).
    Returns the number of failure rows written. With no failures, only the
    header is emitted (the file is still created — the caller asked to save).
    """
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
