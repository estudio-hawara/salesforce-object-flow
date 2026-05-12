"""CSV placeholder regex + typed substitution helpers.

Shared by :mod:`services.composite` and :mod:`services.serial`: both
substitute ``{{column}}`` tokens against a CSV row using the linked
:class:`FileFormat` for type coercion. References (``@{ref.path}``) are
parsed here for validation purposes but resolved by each caller — the
Composite renderer leaves them as-is (Salesforce resolves them
server-side), while the Serial renderer resolves them client-side
against prior step responses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat

PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
REFERENCE_RE: Final[re.Pattern[str]] = re.compile(r"@\{\s*([A-Za-z][A-Za-z0-9_]*)\.[^}]+?\s*\}")
PLACEHOLDER_ONLY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\{\{\s*([^{}]+?)\s*\}\}\s*$")

_BOOLEAN_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_BOOLEAN_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no"})


def render_csv_string(
    value: str,
    column_by_name: Mapping[str, Column],
    csv_row: Mapping[str, str],
) -> object:
    """Render ``value`` substituting ``{{col}}`` tokens against *csv_row*.

    A string that is *only* a single ``{{col}}`` placeholder returns the
    coerced typed value (int, float, bool, str). Anything else (literal
    text, multiple placeholders, mixed content) returns a string with each
    ``{{col}}`` replaced by its raw cell value.
    """
    only = PLACEHOLDER_ONLY_RE.match(value)
    if only is not None:
        token = only.group(1).strip()
        column = column_by_name.get(token)
        if column is None:
            return value
        raw = csv_row.get(token, "")
        return coerce(raw, column)
    return PLACEHOLDER_RE.sub(lambda m: csv_row.get(m.group(1).strip(), m.group(0)), value)


def coerce(raw: str, column: Column) -> object:
    """Coerce a raw CSV cell into a Python value matching ``column.type``.

    Falls back to the original string on conversion failure: the executor
    will surface the underlying Salesforce error rather than mask it here.
    """
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


def synthetic_value(column: Column) -> str:
    """Return a sample raw cell value for ``column``, used by preview rendering."""
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


def synthetic_row(fmt: FileFormat) -> dict[str, str]:
    """Build a synthetic ``{column_name: sample_value}`` dict for previewing."""
    return {col.name: synthetic_value(col) for col in fmt.columns}
