"""CRUD tests for ``services/serial.py:SerialDefinitionStore``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from salesforce_object_flow.core.serial import (
    HttpMethod,
    SerialDefinition,
    SerialStep,
)
from salesforce_object_flow.services.errors import ErrorCode
from salesforce_object_flow.services.serial import (
    SerialDefinitionError,
    SerialDefinitionStore,
)


@pytest.fixture
def tmp_serials_dir(tmp_path: Path) -> Path:
    root = tmp_path / "serials"
    root.mkdir()
    return root


def _def(name: str = "My import") -> SerialDefinition:
    return SerialDefinition(
        name=name,
        format_filename="fmt.json",
        steps=[
            SerialStep(
                reference_id="Q",
                method=HttpMethod.GET,
                url="/services/data/v63.0/query/?q=SELECT+Id",
            )
        ],
    )


def test_save_and_load(tmp_serials_dir: Path) -> None:
    store = SerialDefinitionStore(root=tmp_serials_dir)
    filename = store.save(_def("My import"), previous_filename=None)
    assert filename == "my-import.json"
    loaded = store.load(filename)
    assert loaded is not None
    assert loaded.definition.name == "My import"


def test_list_sorts_by_name(tmp_serials_dir: Path) -> None:
    store = SerialDefinitionStore(root=tmp_serials_dir)
    store.save(_def("Bravo"), previous_filename=None)
    store.save(_def("Alpha"), previous_filename=None)
    listed = store.list_definitions()
    assert [ld.definition.name for ld in listed] == ["Alpha", "Bravo"]


def test_rename_removes_old_file(tmp_serials_dir: Path) -> None:
    store = SerialDefinitionStore(root=tmp_serials_dir)
    old = store.save(_def("Old name"), previous_filename=None)
    definition = _def("New name")
    new = store.save(definition, previous_filename=old)
    assert new == "new-name.json"
    assert not (tmp_serials_dir / old).exists()
    assert (tmp_serials_dir / new).exists()


def test_unique_filename_disambiguates(tmp_serials_dir: Path) -> None:
    store = SerialDefinitionStore(root=tmp_serials_dir)
    store.save(_def("Same"), previous_filename=None)
    duplicate = store.save(_def("Same"), previous_filename=None)
    assert duplicate == "same-2.json"


def test_malformed_files_are_skipped_on_list(tmp_serials_dir: Path) -> None:
    (tmp_serials_dir / "garbage.json").write_text("not json at all")
    (tmp_serials_dir / "empty.json").write_text(json.dumps({"name": ""}))
    store = SerialDefinitionStore(root=tmp_serials_dir)
    store.save(_def("Good"), previous_filename=None)
    assert [ld.definition.name for ld in store.list_definitions()] == ["Good"]


def test_delete(tmp_serials_dir: Path) -> None:
    store = SerialDefinitionStore(root=tmp_serials_dir)
    filename = store.save(_def(), previous_filename=None)
    assert store.delete(filename) is True
    assert store.delete(filename) is False  # already gone


def test_save_failure_raises_coded_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Point the store at a path inside a directory we make read-only afterwards.
    root = tmp_path / "ro"
    root.mkdir()
    store = SerialDefinitionStore(root=root)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(SerialDefinitionError) as exc:
        store.save(_def(), previous_filename=None)
    assert exc.value.code is ErrorCode.SERIAL_SAVE_FAILED
