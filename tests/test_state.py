"""AppState dirty-tracking tests."""

from __future__ import annotations

from salesforce_object_flow.core.state import AppState


def test_register_and_initial_state() -> None:
    state = AppState()
    state.register("name", default="", saved="Acme")

    assert state.get("name").live == "Acme"
    assert state.get("name").saved == "Acme"
    assert state.get("name").default == ""
    assert state.is_dirty is False


def test_set_live_marks_dirty_and_notifies() -> None:
    state = AppState()
    state.register("name", default="")

    notifications: list[str] = []
    state.on_change(notifications.append)

    state.set_live("name", "Acme Inc.")

    assert state.is_dirty is True
    assert state.dirty_keys() == ["name"]
    assert notifications == ["name"]


def test_idempotent_set_live_does_not_notify() -> None:
    state = AppState()
    state.register("name", default="")

    notifications: list[str] = []
    state.on_change(notifications.append)

    state.set_live("name", "")  # same as current live value

    assert notifications == []


def test_mark_saved_clears_dirty() -> None:
    state = AppState()
    state.register("name", default="")
    state.set_live("name", "Acme")

    assert state.is_dirty is True
    state.mark_saved()

    assert state.is_dirty is False
    assert state.get("name").saved == "Acme"


def test_discard_reverts_live_to_saved() -> None:
    state = AppState()
    state.register("name", default="", saved="Acme")
    state.set_live("name", "Globex")

    state.discard()

    assert state.get("name").live == "Acme"
    assert state.is_dirty is False


def test_register_duplicate_raises() -> None:
    state = AppState()
    state.register("name", default="")

    try:
        state.register("name", default="x")
    except ValueError as exc:
        assert "name" in str(exc)
    else:
        raise AssertionError("Expected ValueError on duplicate register")
