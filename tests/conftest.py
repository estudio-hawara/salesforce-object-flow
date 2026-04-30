"""Shared pytest fixtures.

Tests that require GTK4/libadwaita typelibs should opt in at module scope::

    import pytest
    pytest.importorskip("gi")
"""
