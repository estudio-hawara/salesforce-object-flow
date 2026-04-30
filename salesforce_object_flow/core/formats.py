"""File format definitions.

Pure data + JSON. GTK-free. A :class:`FileFormat` describes the shape of a
CSV file the user wants to import: delimiter, quote char, encoding, header
flag, and a list of typed columns. Stored on disk as one JSON file per
format under ``platformdirs.user_data_dir / formats/`` (handled by
:class:`services.formats.FileFormatStore`).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, cast

log = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 1
SUPPORTED_ENCODINGS: Final[tuple[str, ...]] = ("utf-8", "latin-1", "cp1252")


class ColumnType(StrEnum):
    """The set of types a column can declare. ``StrEnum`` round-trips via JSON."""

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    EMAIL = "email"


@dataclass(frozen=True, slots=True)
class Column:
    """One column in a :class:`FileFormat`."""

    name: str
    type: ColumnType
    nullable: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Column | None:
        try:
            type_value = str(data["type"])
            try:
                column_type = ColumnType(type_value)
            except ValueError:
                log.warning("Dropping column with unknown type: %r", type_value)
                return None
            name = str(data["name"]).strip()
            if not name:
                log.warning("Dropping column with empty name")
                return None
            return cls(
                name=name,
                type=column_type,
                nullable=bool(data.get("nullable", True)),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed column entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type.value, "nullable": self.nullable}


@dataclass(slots=True)
class FileFormat:
    """A CSV file shape. Mutated in place by the editor; copy-compared for dirty state."""

    name: str
    description: str = ""
    delimiter: str = ","
    quote_char: str = '"'
    has_header: bool = True
    encoding: str = "utf-8"
    columns: list[Column] = field(default_factory=list[Column])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FileFormat | None:
        try:
            name = str(data["name"]).strip()
            if not name:
                log.warning("Dropping format with empty name")
                return None
            raw_columns = data.get("columns", [])
            columns: list[Column] = []
            if isinstance(raw_columns, list):
                for raw in cast(list[Any], raw_columns):
                    if isinstance(raw, dict):
                        column = Column.from_dict(cast(Mapping[str, Any], raw))
                        if column is not None:
                            columns.append(column)
            return cls(
                name=name,
                description=str(data.get("description", "")),
                delimiter=str(data.get("delimiter", ",")),
                quote_char=str(data.get("quote_char", '"')),
                has_header=bool(data.get("has_header", True)),
                encoding=str(data.get("encoding", "utf-8")),
                columns=columns,
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed file-format entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "delimiter": self.delimiter,
            "quote_char": self.quote_char,
            "has_header": self.has_header,
            "encoding": self.encoding,
            "columns": [column.to_dict() for column in self.columns],
        }


_SLUG_FALLBACK: Final[str] = "format"
_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase ASCII slug. Strips diacritics and punctuation; collapses runs.

    Empty result falls back to ``"format"``.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.casefold()).strip("-")
    return slug or _SLUG_FALLBACK
