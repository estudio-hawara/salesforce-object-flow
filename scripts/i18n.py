"""Translation pipeline wrapper around ``pybabel``.

Subcommands:
- ``extract``: scan source files listed in ``po/POTFILES.in`` and
  produce ``po/salesforce-object-flow.pot``.
- ``update``: merge the latest .pot into each ``po/<lang>.po`` listed
  in ``po/LINGUAS``.
- ``compile``: write ``locale/<lang>/LC_MESSAGES/<domain>.mo`` for each
  ``po/<lang>.po``.

Run as ``uv run python scripts/i18n.py <subcommand>``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PO_DIR = REPO_ROOT / "po"
LOCALE_DIR = REPO_ROOT / "locale"
DOMAIN = "salesforce-object-flow"

POT_PATH = PO_DIR / f"{DOMAIN}.pot"
POTFILES_IN = PO_DIR / "POTFILES.in"
LINGUAS = PO_DIR / "LINGUAS"
BABEL_CFG = PO_DIR / "babel.cfg"


def _read_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _languages() -> list[str]:
    if not LINGUAS.exists():
        return []
    return _read_lines(LINGUAS)


def _input_files() -> list[Path]:
    if not POTFILES_IN.exists():
        return []
    return [REPO_ROOT / line for line in _read_lines(POTFILES_IN)]


def cmd_extract() -> int:
    files = _input_files()
    if not files:
        print(f"No input files listed in {POTFILES_IN}.", file=sys.stderr)
        return 1
    POT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call(
        [
            "pybabel",
            "extract",
            "-F",
            str(BABEL_CFG),
            "-k",
            "_",
            "-k",
            "N_",
            "-o",
            str(POT_PATH),
            *(str(f) for f in files),
        ],
        cwd=REPO_ROOT,
    )


def cmd_update() -> int:
    if not POT_PATH.exists():
        print(f"Run 'extract' first; {POT_PATH} does not exist.", file=sys.stderr)
        return 1
    rc = 0
    for lang in _languages():
        po_path = PO_DIR / f"{lang}.po"
        if not po_path.exists():
            rc |= subprocess.call(
                [
                    "pybabel",
                    "init",
                    "-i",
                    str(POT_PATH),
                    "-o",
                    str(po_path),
                    "-l",
                    lang,
                ],
                cwd=REPO_ROOT,
            )
        else:
            rc |= subprocess.call(
                [
                    "pybabel",
                    "update",
                    "-i",
                    str(POT_PATH),
                    "-o",
                    str(po_path),
                    "-l",
                    lang,
                ],
                cwd=REPO_ROOT,
            )
    return rc


def cmd_compile() -> int:
    rc = 0
    for lang in _languages():
        po_path = PO_DIR / f"{lang}.po"
        if not po_path.exists():
            print(f"Skipping {lang}: {po_path} not found.", file=sys.stderr)
            continue
        mo_path = LOCALE_DIR / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"
        mo_path.parent.mkdir(parents=True, exist_ok=True)
        rc |= subprocess.call(
            [
                "pybabel",
                "compile",
                "-i",
                str(po_path),
                "-o",
                str(mo_path),
                "-l",
                lang,
            ],
            cwd=REPO_ROOT,
        )
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("extract", help="Generate the .pot template")
    sub.add_parser("update", help="Merge .pot into each language .po")
    sub.add_parser("compile", help="Compile each .po into a .mo")
    args = parser.parse_args()
    handlers = {
        "extract": cmd_extract,
        "update": cmd_update,
        "compile": cmd_compile,
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
