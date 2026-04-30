"""Tests for ``services/formats.py:FileFormatStore``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.services.formats import FileFormatError, FileFormatStore


def _format(name: str = "Customer extract", **overrides: object) -> FileFormat:
    defaults: dict[str, object] = {
        "name": name,
        "description": "",
        "delimiter": ",",
        "quote_char": '"',
        "has_header": True,
        "encoding": "utf-8",
        "columns": [Column(name="id", type=ColumnType.INTEGER, nullable=False)],
    }
    defaults.update(overrides)
    return FileFormat(**defaults)  # type: ignore[arg-type]


def test_save_and_load_round_trip(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    fmt = _format()

    filename = store.save(fmt, previous_filename=None)

    assert filename == "customer-extract.json"
    listed = store.list_formats()
    assert len(listed) == 1
    assert listed[0].format == fmt
    assert listed[0].filename == "customer-extract.json"


def test_save_creates_directory(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    store = FileFormatStore(root=root)
    store.save(_format(), previous_filename=None)
    assert root.is_dir()


def test_save_rename_deletes_old_file(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    first = store.save(_format("Original"), previous_filename=None)
    fmt = _format("Renamed")
    second = store.save(fmt, previous_filename=first)

    assert second == "renamed.json"
    assert not (tmp_formats_dir / first).exists()
    assert (tmp_formats_dir / second).exists()


def test_save_same_slug_no_unlink(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    filename = store.save(_format(), previous_filename=None)
    fmt = _format(description="Updated description")

    again = store.save(fmt, previous_filename=filename)

    assert again == filename
    payload = json.loads((tmp_formats_dir / filename).read_text(encoding="utf-8"))
    assert payload["description"] == "Updated description"


def test_save_collision_with_other_format_appends_suffix(
    tmp_formats_dir: Path,
) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    store.save(_format("My format"), previous_filename=None)

    second_filename = store.save(_format("My Format"), previous_filename=None)

    assert second_filename == "my-format-2.json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod test")
def test_save_atomic_no_partial_file_on_error(tmp_path: Path) -> None:
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        store = FileFormatStore(root=root)
        with pytest.raises(FileFormatError):
            store.save(_format(), previous_filename=None)
        # No .tmp left behind.
        leftovers = [p for p in root.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []
    finally:
        root.chmod(0o700)


def test_load_skips_malformed_json(tmp_formats_dir: Path) -> None:
    (tmp_formats_dir / "broken.json").write_text("{not json", encoding="utf-8")
    store = FileFormatStore(root=tmp_formats_dir)

    assert store.list_formats() == []


def test_load_skips_missing_required_fields(tmp_formats_dir: Path) -> None:
    (tmp_formats_dir / "incomplete.json").write_text(
        json.dumps({"description": "no name"}), encoding="utf-8"
    )
    store = FileFormatStore(root=tmp_formats_dir)

    assert store.list_formats() == []


def test_load_skips_non_dict_root(tmp_formats_dir: Path) -> None:
    (tmp_formats_dir / "weird.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    store = FileFormatStore(root=tmp_formats_dir)

    assert store.list_formats() == []


def test_load_ignores_non_json_files(tmp_formats_dir: Path) -> None:
    (tmp_formats_dir / "notes.txt").write_text("hello", encoding="utf-8")
    store = FileFormatStore(root=tmp_formats_dir)
    store.save(_format(), previous_filename=None)

    assert [lf.filename for lf in store.list_formats()] == ["customer-extract.json"]


def test_delete_removes_file(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    filename = store.save(_format(), previous_filename=None)

    assert store.delete(filename) is True
    assert store.list_formats() == []


def test_delete_missing_file_returns_false(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    assert store.delete("nope.json") is False


def test_unique_filename_for_appends_suffix(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)

    assert store.unique_filename_for("Foo", existing=set()) == "foo.json"
    assert store.unique_filename_for("Foo", existing={"foo.json"}) == "foo-2.json"
    assert store.unique_filename_for("Foo", existing={"foo.json", "foo-2.json"}) == "foo-3.json"


def test_list_sorted_by_name(tmp_formats_dir: Path) -> None:
    store = FileFormatStore(root=tmp_formats_dir)
    store.save(_format("Zebra"), previous_filename=None)
    store.save(_format("Alpha"), previous_filename=None)
    store.save(_format("Mango"), previous_filename=None)

    listed = [lf.format.name for lf in store.list_formats()]
    assert listed == ["Alpha", "Mango", "Zebra"]
