# Contributing to Salesforce Object Flow

Thanks for your interest in contributing. This document covers the local
development workflow, the checks CI runs, and the code style we expect.

## Development prerequisites

The runtime stack is GTK4 + libadwaita + PyGObject. Install the system
libraries before running `uv sync`.

### Linux (Debian / Ubuntu)

```bash
sudo apt install libcairo2-dev libgirepository-2.0-dev libgtk-4-dev libadwaita-1-dev
```

On Fedora / Arch / openSUSE, install the equivalent `gtk4`, `libadwaita`,
`gobject-introspection`, and `cairo` development packages.

### macOS

```bash
brew install gtk4 libadwaita gobject-introspection pygobject3
```

### Windows

Install [MSYS2](https://www.msys2.org/) and from the **UCRT64** shell:

```bash
pacman -S mingw-w64-ucrt-x86_64-gtk4 \
          mingw-w64-ucrt-x86_64-libadwaita \
          mingw-w64-ucrt-x86_64-python \
          mingw-w64-ucrt-x86_64-python-gobject \
          mingw-w64-ucrt-x86_64-python-pip
```

All `uv` commands below must be run from the UCRT64 shell.

## Workflow

```bash
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow
uv sync
uv run salesforce-object-flow
```

## The four checks

CI runs these four commands. Run them locally before opening a pull request.

```bash
uv run ruff check salesforce_object_flow/ tests/
uv run ruff format --check salesforce_object_flow/ tests/
uv run pyright salesforce_object_flow/ tests/
uv run pytest tests/ -v
```

Auto-fix the formatter / lint findings with:

```bash
uv run ruff format salesforce_object_flow/ tests/
uv run ruff check --fix salesforce_object_flow/ tests/
```

## Code style

- **Formatter / linter**: `ruff` with `select = ["E", "F", "W", "I"]`,
  `line-length = 100`, `target-version = "py312"`.
- **Type checker**: `pyright` in strict mode for the package.
- **Type narrowing**: don't use `assert` for it — prefer `isinstance` checks,
  early returns, or `cast` when the runtime invariant is clear.
- **Comments**: write one only when the *why* is non-obvious. Don't explain
  what the code does — naming and types should carry that.
- **Async / threads**: use `GLib.idle_add` and the `Timer` helper for deferred
  work. No `asyncio`, no manual threads in v1.
- **Logging**: one logger per module via
  `import logging; log = logging.getLogger(__name__)`.

## Translations

The app uses Python's `gettext` at runtime and `babel` (`pybabel`) at
development time. The source language is English; translations live in
`po/<lang>.po`.

- **Marking strings**:
  - Inside functions / methods: wrap with `_()`.
  - At module or class scope (constants, `ClassVar`, `Enum` values):
    wrap with `N_()` so extraction picks them up, then call `_()` at the
    use-site. The `tests/test_i18n.py` regression guard fails the build
    if a `_()` call sneaks into module / class scope.
  - For runtime values, prefer `_("Hello {name}").format(name=...)` over
    f-strings so the `msgid` stays stable.

- **Workflow**:

  ```bash
  uv run python scripts/i18n.py extract     # source/.py → po/salesforce-object-flow.pot
  uv run python scripts/i18n.py update      # merge .pot into each po/<lang>.po
  uv run python scripts/i18n.py compile     # po/<lang>.po → locale/<lang>/LC_MESSAGES/<domain>.mo
  ```

- **Adding a new language**: append the locale code (e.g. `fr`, `de_DE`)
  to `po/LINGUAS`, run `extract` then `update` — pybabel will create
  `po/<lang>.po` populated with the empty `msgstr ""` skeleton. Translate
  and `compile`. A first run can also use `pybabel init -l <lang>` if you
  prefer to seed the file manually.

- **Verifying locally**: `LANGUAGE=es uv run salesforce-object-flow` (or
  any other locale code that has a compiled catalog under `locale/`).

- **Service errors**: errors carry an `ErrorCode` and are translated at
  the toast boundary by `i18n_errors.format_error`. To add a new
  user-facing error, add a value to `ErrorCode`, register a formatter in
  `i18n_errors._TEMPLATES`, and pass `code=ErrorCode.X` + `params={…}`
  on the `raise`. The English message you pass positionally still
  surfaces in logs and bug reports.

The `locale/` directory is git-ignored — `.mo` artifacts are regenerated
from `.po` (the source of truth). Translators only need to touch
`po/<lang>.po`.

## Scope notes

Version 1 focuses on the Composite REST API: building, validating, and
submitting one transactional multi-object create. Other Salesforce APIs
(Bulk, Streaming, Metadata) and broader features (org migrations, deploy
flows) are out of scope until v1 ships.

For larger changes, please open an issue first to discuss the approach.
