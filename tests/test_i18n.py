"""Regression guards for the i18n discipline.

Two checks:

1. No ``_(...)`` calls at module or class body scope. Those evaluate at
   import time (module body) or class definition time (class body, also
   import time), which would freeze translations against whatever locale
   was active at first import. Use ``N_(...)`` at definition and
   ``_(...)`` at use-site.

2. ``format_error`` round-trips known ``ErrorCode`` values into translated
   messages without crashing on the formatter table.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from salesforce_object_flow import i18n
from salesforce_object_flow.i18n_errors import _TEMPLATES, format_error
from salesforce_object_flow.services.errors import CodedError, ErrorCode

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "salesforce_object_flow"


def _module_level_underscore_calls(source: str) -> list[int]:
    """Return line numbers of ``_(...)`` calls outside any function body.

    ``ClassDef`` bodies count as "outside a function" because they execute
    at import time, the same trap as module-level code.
    """
    tree = ast.parse(source)
    offenders: list[int] = []

    def walk(node: ast.AST, *, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                walk(child, inside_function=True)
            elif isinstance(child, ast.Call):
                if (
                    not inside_function
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "_"
                ):
                    offenders.append(child.lineno)
                walk(child, inside_function=inside_function)
            else:
                walk(child, inside_function=inside_function)

    walk(tree, inside_function=False)
    return offenders


def test_no_module_or_class_level_underscore_calls() -> None:
    """``_()`` at import-evaluated scopes is forbidden — use ``N_()`` instead."""
    findings: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        # The i18n_errors templates are functions that evaluate `_()` only
        # at call time, so they're fine. But the module-level dict does
        # contain function references — never bare `_()` calls.
        source = path.read_text(encoding="utf-8")
        for lineno in _module_level_underscore_calls(source):
            findings.append(
                f"{path.relative_to(PACKAGE_ROOT.parent)}:{lineno}: "
                f"`_()` at module/class scope (use `N_()` here, `_()` at use-site)"
            )
    assert not findings, "\n  " + "\n  ".join(findings)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_format_error_has_template_for_every_code(code: ErrorCode) -> None:
    """Every ``ErrorCode`` must have a formatter in ``_TEMPLATES``."""
    assert code in _TEMPLATES, f"Missing template for {code.name}"


def test_format_error_falls_back_to_str_when_no_code() -> None:
    """``format_error`` returns ``str(exc)`` when ``code`` is absent."""
    exc = RuntimeError("plain message, no code")
    assert format_error(exc) == "plain message, no code"


def test_format_error_renders_coded_exception() -> None:
    """End-to-end: a ``CodedError`` with params produces a localized string."""
    i18n.init()
    exc = CodedError(
        "english fallback",
        code=ErrorCode.ALIAS_ALREADY_EXISTS,
        params={"alias": "prod"},
    )
    rendered = format_error(exc)
    # Both the source string and its Spanish translation should mention the alias.
    assert "prod" in rendered
    # When LANGUAGE=es is active, the string should be Spanish; otherwise English.
    # We don't assert the locale here — only the round-trip with params.
