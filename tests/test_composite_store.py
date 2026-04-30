"""Tests for ``services/composite.py:CompositeTemplateStore``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from salesforce_object_flow.core.composite import (
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)
from salesforce_object_flow.services.composite import (
    CompositeTemplateError,
    CompositeTemplateStore,
)


def _template(name: str = "Customer create", **overrides: object) -> CompositeTemplate:
    defaults: dict[str, object] = {
        "name": name,
        "description": "",
        "format_filename": "customer.json",
        "all_or_none": True,
        "collate_subrequests": False,
        "subrequests": [
            Subrequest(
                reference_id="newAccount",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/Account",
                body=[BodyField(field="Name", value="{{company}}")],
            )
        ],
    }
    defaults.update(overrides)
    return CompositeTemplate(**defaults)  # type: ignore[arg-type]


def test_save_and_load_round_trip(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    tpl = _template()

    filename = store.save(tpl, previous_filename=None)

    assert filename == "customer-create.json"
    listed = store.list_templates()
    assert len(listed) == 1
    assert listed[0].template == tpl
    assert listed[0].filename == "customer-create.json"


def test_save_creates_directory(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    store = CompositeTemplateStore(root=root)
    store.save(_template(), previous_filename=None)
    assert root.is_dir()


def test_save_rename_deletes_old_file(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    first = store.save(_template("Original"), previous_filename=None)
    second = store.save(_template("Renamed"), previous_filename=first)

    assert second == "renamed.json"
    assert not (tmp_templates_dir / first).exists()
    assert (tmp_templates_dir / second).exists()


def test_save_same_slug_no_unlink(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    filename = store.save(_template(), previous_filename=None)
    again = store.save(_template(description="Updated description"), previous_filename=filename)

    assert again == filename
    payload = json.loads((tmp_templates_dir / filename).read_text(encoding="utf-8"))
    assert payload["description"] == "Updated description"


def test_save_collision_with_other_template_appends_suffix(
    tmp_templates_dir: Path,
) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    store.save(_template("My template"), previous_filename=None)

    second_filename = store.save(_template("My Template"), previous_filename=None)

    assert second_filename == "my-template-2.json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod test")
def test_save_atomic_no_partial_file_on_error(tmp_path: Path) -> None:
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        store = CompositeTemplateStore(root=root)
        with pytest.raises(CompositeTemplateError):
            store.save(_template(), previous_filename=None)
        leftovers = [p for p in root.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []
    finally:
        root.chmod(0o700)


def test_load_skips_malformed_json(tmp_templates_dir: Path) -> None:
    (tmp_templates_dir / "broken.json").write_text("{not json", encoding="utf-8")
    store = CompositeTemplateStore(root=tmp_templates_dir)

    assert store.list_templates() == []


def test_load_skips_missing_required_fields(tmp_templates_dir: Path) -> None:
    (tmp_templates_dir / "incomplete.json").write_text(
        json.dumps({"description": "no name"}), encoding="utf-8"
    )
    store = CompositeTemplateStore(root=tmp_templates_dir)

    assert store.list_templates() == []


def test_load_skips_non_dict_root(tmp_templates_dir: Path) -> None:
    (tmp_templates_dir / "weird.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    store = CompositeTemplateStore(root=tmp_templates_dir)

    assert store.list_templates() == []


def test_load_ignores_non_json_files(tmp_templates_dir: Path) -> None:
    (tmp_templates_dir / "notes.txt").write_text("hello", encoding="utf-8")
    store = CompositeTemplateStore(root=tmp_templates_dir)
    store.save(_template(), previous_filename=None)

    assert [lt.filename for lt in store.list_templates()] == ["customer-create.json"]


def test_delete_removes_file(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    filename = store.save(_template(), previous_filename=None)

    assert store.delete(filename) is True
    assert store.list_templates() == []


def test_delete_missing_file_returns_false(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    assert store.delete("nope.json") is False


def test_unique_filename_for_appends_suffix(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)

    assert store.unique_filename_for("Foo", existing=set()) == "foo.json"
    assert store.unique_filename_for("Foo", existing={"foo.json"}) == "foo-2.json"
    assert store.unique_filename_for("Foo", existing={"foo.json", "foo-2.json"}) == "foo-3.json"


def test_list_sorted_by_name(tmp_templates_dir: Path) -> None:
    store = CompositeTemplateStore(root=tmp_templates_dir)
    store.save(_template("Zebra"), previous_filename=None)
    store.save(_template("Alpha"), previous_filename=None)
    store.save(_template("Mango"), previous_filename=None)

    listed = [lt.template.name for lt in store.list_templates()]
    assert listed == ["Alpha", "Mango", "Zebra"]
