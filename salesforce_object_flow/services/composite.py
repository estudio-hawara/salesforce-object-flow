"""Composite template storage + validator + payload renderer.

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

All three stay GTK-free and synchronous.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from platformdirs import PlatformDirs

from salesforce_object_flow.core.composite import (
    MAX_SUBREQUESTS,
    METHODS_WITH_BODY,
    REFERENCE_ID_RE,
    CompositeTemplate,
    Subrequest,
)
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat, slugify

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
