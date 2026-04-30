"""Shared pytest fixtures.

Tests that require GTK4/libadwaita typelibs should opt in at module scope::

    import pytest
    pytest.importorskip("gi")
"""

from __future__ import annotations

from pathlib import Path

import pytest

from salesforce_object_flow.core import credentials as credentials_module
from salesforce_object_flow.core.cache import JsonCache


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if (service, key) not in self.store:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("not found")
        del self.store[(service, key)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials_module, "keyring", fake)
    return fake


@pytest.fixture
def tmp_cache(tmp_path: Path) -> JsonCache:
    return JsonCache(root=tmp_path / "cache")


@pytest.fixture
def tmp_formats_dir(tmp_path: Path) -> Path:
    root = tmp_path / "formats"
    root.mkdir()
    return root


@pytest.fixture
def tmp_templates_dir(tmp_path: Path) -> Path:
    root = tmp_path / "templates"
    root.mkdir()
    return root
