"""Tests for ``services/formats.py:FileFormatValidator``."""

from __future__ import annotations

from pathlib import Path

from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.services.formats import FileFormatValidator


def _format(*columns: Column, **overrides: object) -> FileFormat:
    defaults: dict[str, object] = {
        "name": "Test format",
        "description": "",
        "delimiter": ",",
        "quote_char": '"',
        "has_header": True,
        "encoding": "utf-8",
        "columns": list(columns),
    }
    defaults.update(overrides)
    return FileFormat(**defaults)  # type: ignore[arg-type]


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _validator() -> FileFormatValidator:
    return FileFormatValidator()


def test_valid_string_only(tmp_path: Path) -> None:
    fmt = _format(Column(name="note", type=ColumnType.STRING))
    file = _write(tmp_path / "f.csv", ["note", "alpha", "beta", "gamma"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()
    assert report.rows_examined == 3
    assert report.fatal is None


def test_integer_invalid(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER))
    file = _write(tmp_path / "f.csv", ["id", "1", "abc", "3"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    err = report.errors[0]
    assert err.row == 3
    assert err.column == "id"
    assert err.value == "abc"
    assert "integer" in err.message


def test_integer_decimal_rejected(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER))
    file = _write(tmp_path / "f.csv", ["id", "1.5"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1


def test_decimal_accepts_int_and_decimal(tmp_path: Path) -> None:
    fmt = _format(Column(name="amount", type=ColumnType.DECIMAL))
    file = _write(tmp_path / "f.csv", ["amount", "1", "1.5", "0.001"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()


def test_boolean_accepts_canonical(tmp_path: Path) -> None:
    fmt = _format(Column(name="active", type=ColumnType.BOOLEAN))
    file = _write(tmp_path / "f.csv", ["active", "true", "FALSE", "1", "0", "Yes", "no"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()


def test_boolean_rejects_other(tmp_path: Path) -> None:
    fmt = _format(Column(name="active", type=ColumnType.BOOLEAN))
    file = _write(tmp_path / "f.csv", ["active", "maybe"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert "boolean" in report.errors[0].message


def test_date_iso_only(tmp_path: Path) -> None:
    fmt = _format(Column(name="d", type=ColumnType.DATE))
    file = _write(tmp_path / "f.csv", ["d", "2026-04-30", "30/04/2026"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert report.errors[0].row == 3


def test_datetime_iso(tmp_path: Path) -> None:
    fmt = _format(Column(name="t", type=ColumnType.DATETIME))
    file = _write(tmp_path / "f.csv", ["t", "2026-04-30T12:00:00"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()


def test_email_basic(tmp_path: Path) -> None:
    fmt = _format(Column(name="e", type=ColumnType.EMAIL))
    file = _write(tmp_path / "f.csv", ["e", "a@b.com", "not-email"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert "email" in report.errors[0].message


def test_nullable_empty_ok(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER, nullable=True))
    file = _write(tmp_path / "f.csv", ["id", "1", "", "3"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()


def test_non_nullable_empty_errors(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER, nullable=False))
    file = _write(tmp_path / "f.csv", ["id", "1", "", "3"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert "Required" in report.errors[0].message


def test_row_too_few_columns(tmp_path: Path) -> None:
    fmt = _format(
        Column(name="a", type=ColumnType.STRING),
        Column(name="b", type=ColumnType.STRING),
        Column(name="c", type=ColumnType.STRING),
    )
    file = _write(tmp_path / "f.csv", ["a,b,c", "1,2"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert report.errors[0].column == ""
    assert "Expected 3" in report.errors[0].message


def test_row_too_many_columns(tmp_path: Path) -> None:
    fmt = _format(
        Column(name="a", type=ColumnType.STRING),
        Column(name="b", type=ColumnType.STRING),
    )
    file = _write(tmp_path / "f.csv", ["a,b", "1,2,3"])

    report = _validator().validate(fmt, file)

    assert len(report.errors) == 1
    assert "got 3" in report.errors[0].message


def test_has_header_skips_first_row(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER))
    file = _write(tmp_path / "f.csv", ["id", "1", "2"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()
    assert report.rows_examined == 2


def test_no_header_validates_first_row(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER), has_header=False)
    file = _write(tmp_path / "f.csv", ["id", "1", "2"])

    report = _validator().validate(fmt, file)

    # First row is "id" which is not a valid integer.
    assert len(report.errors) == 1
    assert report.errors[0].row == 1


def test_caps_at_max_rows(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER))
    rows = ["id"] + [str(i) for i in range(200)]
    file = _write(tmp_path / "f.csv", rows)

    report = _validator().validate(fmt, file)

    assert report.rows_examined == FileFormatValidator.MAX_ROWS
    assert report.truncated is True


def test_caps_at_max_errors(tmp_path: Path) -> None:
    fmt = _format(Column(name="id", type=ColumnType.INTEGER))
    rows = ["id"] + ["abc"] * 60
    file = _write(tmp_path / "f.csv", rows)

    report = _validator().validate(fmt, file)

    assert len(report.errors) == FileFormatValidator.MAX_ERRORS
    assert report.truncated is True


def test_encoding_failure_is_fatal(tmp_path: Path) -> None:
    fmt = _format(Column(name="note", type=ColumnType.STRING))
    file = tmp_path / "f.csv"
    # Latin-1 byte that's invalid as UTF-8.
    file.write_bytes(b"note\n\xff\xfe\n")

    report = _validator().validate(fmt, file)

    assert report.fatal is not None
    assert "utf-8" in report.fatal
    assert report.errors == ()


def test_custom_delimiter_and_quote(tmp_path: Path) -> None:
    fmt = _format(
        Column(name="a", type=ColumnType.STRING),
        Column(name="b", type=ColumnType.INTEGER),
        delimiter=";",
        quote_char="'",
    )
    file = _write(tmp_path / "f.csv", ["a;b", "'x;y';2"])

    report = _validator().validate(fmt, file)

    assert report.errors == ()
