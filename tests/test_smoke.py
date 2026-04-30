"""Smoke imports: every module in the package must load without errors."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

pytest.importorskip("gi")

import salesforce_object_flow  # noqa: E402


def _module_names() -> list[str]:
    package = salesforce_object_flow
    names: list[str] = [package.__name__]
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        names.append(module_info.name)
    return names


@pytest.mark.parametrize("module_name", _module_names())
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
