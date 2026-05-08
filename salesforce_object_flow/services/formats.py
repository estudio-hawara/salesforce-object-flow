"""File-format storage + CSV validator.

:class:`FileFormatStore` does CRUD on the user's ``user_data_dir / formats/``
directory: one JSON file per format, slugified filenames, atomic writes.
Hand-edited corruption is tolerated on read (logged and skipped) but write
failures surface as :class:`FileFormatError` since the user pressed Save and
expects feedback.

:class:`FileFormatValidator` runs a :class:`FileFormat` against a CSV file's
first ``MAX_ROWS`` rows and yields up to ``MAX_ERRORS`` per-cell errors.

Both stay GTK-free; the page is responsible for offloading to a worker thread.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from platformdirs import PlatformDirs

from salesforce_object_flow.core.formats import (
    Column,
    ColumnType,
    FileFormat,
    slugify,
)
from salesforce_object_flow.services.errors import CodedError, ErrorCode

log = logging.getLogger(__name__)

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=True)


class FileFormatError(CodedError):
    """Save / Delete failures that must surface to the user."""


@dataclass(frozen=True, slots=True)
class LoadedFormat:
    """A :class:`FileFormat` paired with its on-disk filename (e.g. ``customer.json``)."""

    format: FileFormat
    filename: str


@dataclass(frozen=True, slots=True)
class CellError:
    """One validation error. ``column == ""`` means a row-shape problem."""

    row: int
    column: str
    value: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    rows_examined: int
    truncated: bool
    errors: tuple[CellError, ...]
    fatal: str | None = None


# ====================================================================
# FileFormatStore
# ====================================================================


def _default_root() -> Path:
    return Path(_DIRS.user_data_dir) / "formats"


class FileFormatStore:
    """Disk-backed CRUD for :class:`FileFormat` JSON files."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _default_root()

    @property
    def root(self) -> Path:
        return self._root

    def list_formats(self) -> list[LoadedFormat]:
        """Return every format on disk, sorted by name. Malformed files skipped."""
        if not self._root.exists():
            return []
        loaded: list[LoadedFormat] = []
        for entry in self._root.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            payload = _safe_load_json(entry)
            if payload is None:
                continue
            fmt = FileFormat.from_dict(payload)
            if fmt is None:
                continue
            loaded.append(LoadedFormat(format=fmt, filename=entry.name))
        loaded.sort(key=lambda lf: lf.format.name.casefold())
        return loaded

    def load(self, filename: str) -> LoadedFormat | None:
        path = self._root / filename
        payload = _safe_load_json(path)
        if payload is None:
            return None
        fmt = FileFormat.from_dict(payload)
        if fmt is None:
            return None
        return LoadedFormat(format=fmt, filename=filename)

    def save(self, fmt: FileFormat, *, previous_filename: str | None) -> str:
        """Persist *fmt* to disk and return the resulting filename.

        If *previous_filename* differs from the new slug-derived filename, the
        old file is removed in a best-effort pass after the new one lands.
        """
        target_filename = self._target_filename(fmt, previous_filename=previous_filename)
        path = self._root / target_filename
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(fmt.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            # Best-effort cleanup of the temp file if it exists.
            try:
                path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass
            raise FileFormatError(
                f"Could not save format: {exc}",
                code=ErrorCode.FORMAT_SAVE_FAILED,
                params={"error": str(exc)},
            ) from exc

        if previous_filename and previous_filename != target_filename:
            previous = self._root / previous_filename
            try:
                previous.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove old format file %s: %s", previous, exc)

        return target_filename

    def delete(self, filename: str) -> bool:
        path = self._root / filename
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise FileFormatError(
                f"Could not delete format: {exc}",
                code=ErrorCode.FORMAT_DELETE_FAILED,
                params={"error": str(exc)},
            ) from exc
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

    def _target_filename(self, fmt: FileFormat, *, previous_filename: str | None) -> str:
        slug = slugify(fmt.name)
        candidate = f"{slug}.json"
        if previous_filename == candidate:
            return candidate

        # Avoid clobbering a different format that already owns the slug.
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
        log.warning("Could not read format file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Format file %s is not a JSON object", path)
        return None
    return cast(dict[str, Any], data)


# ====================================================================
# FileFormatValidator
# ====================================================================


_EMAIL_RE: re.Pattern[str] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BOOLEAN_VALUES: frozenset[str] = frozenset({"true", "false", "1", "0", "yes", "no"})


class FileFormatValidator:
    """Run a :class:`FileFormat` against the first ``MAX_ROWS`` rows of a CSV."""

    MAX_ROWS: ClassVar[int] = 100
    MAX_ERRORS: ClassVar[int] = 50

    def validate(self, fmt: FileFormat, path: Path) -> ValidationReport:
        try:
            text = path.read_text(encoding=fmt.encoding)
        except OSError as exc:
            return ValidationReport(
                rows_examined=0,
                truncated=False,
                errors=(),
                fatal=f"Could not read {path}: {exc}",
            )
        except UnicodeDecodeError as exc:
            return ValidationReport(
                rows_examined=0,
                truncated=False,
                errors=(),
                fatal=f"Could not decode {path} as {fmt.encoding}: {exc}",
            )

        reader = csv.reader(
            text.splitlines(),
            delimiter=fmt.delimiter,
            quotechar=fmt.quote_char,
        )

        errors: list[CellError] = []
        rows_examined = 0
        truncated = False
        column_count = len(fmt.columns)

        for index, row in enumerate(reader, start=1):
            if fmt.has_header and index == 1:
                continue
            if rows_examined >= self.MAX_ROWS:
                truncated = True
                break
            rows_examined += 1

            # csv.reader collapses a blank line to []; interpret that as a row
            # of all-empty cells so nullability rules apply per column.
            if not row and column_count > 0:
                row = [""] * column_count

            if len(row) != column_count:
                if len(errors) < self.MAX_ERRORS:
                    errors.append(
                        CellError(
                            row=index,
                            column="",
                            value="",
                            message=f"Expected {column_count} columns, got {len(row)}.",
                        )
                    )
                if len(errors) >= self.MAX_ERRORS:
                    truncated = True
                    break
                continue

            for column, raw_value in zip(fmt.columns, row, strict=False):
                problem = _validate_cell(column, raw_value)
                if problem is None:
                    continue
                if len(errors) >= self.MAX_ERRORS:
                    truncated = True
                    break
                errors.append(
                    CellError(row=index, column=column.name, value=raw_value, message=problem)
                )
            if len(errors) >= self.MAX_ERRORS:
                truncated = True
                break

        return ValidationReport(
            rows_examined=rows_examined,
            truncated=truncated,
            errors=tuple(errors),
        )


def _validate_cell(column: Column, value: str) -> str | None:
    """Return an error message, or None if the cell is valid for *column*."""
    if value == "":
        if column.nullable:
            return None
        return "Required (not nullable)."

    column_type = column.type
    try:
        if column_type is ColumnType.STRING:
            return None
        if column_type is ColumnType.INTEGER:
            int(value)
            return None
        if column_type is ColumnType.DECIMAL:
            decimal.Decimal(value)
            return None
        if column_type is ColumnType.BOOLEAN:
            if value.casefold() not in _BOOLEAN_VALUES:
                return "Not a valid boolean."
            return None
        if column_type is ColumnType.DATE:
            datetime.date.fromisoformat(value)
            return None
        if column_type is ColumnType.DATETIME:
            datetime.datetime.fromisoformat(value)
            return None
        if column_type is ColumnType.EMAIL:
            if not _EMAIL_RE.match(value):
                return "Not a valid email."
            return None
    except (ValueError, decimal.InvalidOperation):
        return f"Not a valid {column_type.value}."
    return None  # pragma: no cover - exhaustive enum match above
