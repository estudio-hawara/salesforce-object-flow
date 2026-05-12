# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a4] - 2026-05-12

### Added

- **Serial Requests**: a new top-level page (under the *Run* group, next to
  *Composite Requests*) that executes a client-driven sequence of REST calls
  per CSV row. Unlike a Composite template — which produces a single
  transactional `/composite` payload — a serial definition fires *N*
  independent HTTP requests per row, with each step's response resolved
  client-side so later steps can branch on it.
- Per-step **conditions**: each step can carry a `StepCondition` that
  combines one or more `ConditionCheck` predicates with `all_of` / `any_of`.
  The predicate language covers `exists`, `not_exists`, `status_ok`,
  `status_failed`, `eq`, `ne`, `records_count_eq` and `records_count_gt`,
  evaluated against a dotted JSON path inside a prior step's response
  (`records[0].Id`, `id`, …). Steps whose condition does not hold are
  recorded as `skipped`.
- **Client-side `@{ref.path}` resolution**: references in URL, headers, and
  body fields are resolved against prior step results (`@{step.id}`,
  `@{lookup.records[0].Id}`, plus the synthetic `status` / `ok` / `skipped`
  paths). `{{column}}` placeholders keep working alongside references and
  share the same typed coercion (`core.placeholders`) with the Composite
  page.
- **`continue_on_failure`** per step (mirrors Salesforce Composite's
  `allowsFailure`): a failed step records the failure and lets the row
  continue instead of aborting.
- **`SerialDefinitionStore`**, **`SerialDefinitionValidator`**,
  **`SerialStepRenderer`** and **`SerialExecutor`** in
  `services/serial.py`. Definitions are persisted as one JSON file per
  definition under `user_data_dir/serials/`, with the same atomic
  write-then-rename discipline as Composite templates.
- **Failure CSV export** (`export_failures_csv`): after a run, failed rows
  can be re-emitted to a CSV that preserves the original columns plus an
  `_error` column, ready to be cleaned up and re-imported.
- New error codes `SERIAL_SAVE_FAILED` and `SERIAL_DELETE_FAILED`, plus
  localized templates in `i18n_errors.py`.
- Spanish (`es`) translations for the full Serial Requests UI and its
  service-layer error messages.
- Test suite for the new module:
  `tests/test_serial_core.py`, `tests/test_serial_store.py`,
  `tests/test_serial_validator.py`, `tests/test_serial_renderer.py`,
  `tests/test_serial_executor.py`.

### Changed

- Placeholder / reference parsing moved to a shared `core/placeholders.py`
  module so the Composite and Serial pages stay byte-for-byte consistent on
  `{{column}}` substitution and `@{ref.path}` recognition.
- `services/composite.py` and `services/api.py` got minor extension points
  needed by the Serial executor (single-request dispatch, shared CSV reader
  helpers) — the public Composite behaviour is unchanged.

## [0.1.0a3] - 2026-05-08

### Changed

- **Breaking:** Composite request templates are now stored in
  `user_data_dir/composites/` instead of `user_data_dir/templates/`. Existing
  `0.1.0a2` users must move their JSON files manually to keep their saved
  templates available.

## [0.1.0a2] - 2026-05-08

### Added

- Internationalization (i18n) support based on Python's `gettext`. Every
  user-facing string in the UI (window, sidebar, page titles, toasts, dialogs,
  status pages) and in service-layer error messages is now translatable.
- Spanish (`es`) translation covering the full UI: launching with
  `LANGUAGE=es uv run salesforce-object-flow` shows the app in castellano.
- `ErrorCode` enum (`services/errors.py`) and `format_error()` helper
  (`i18n_errors.py`): service exceptions carry a stable code plus a `params`
  dict, the toast layer renders them in the user's language while logs and
  bug reports keep the English message.
- `scripts/i18n.py` developer wrapper around `pybabel` with `extract` /
  `update` / `compile` subcommands, plus `po/POTFILES.in` and `po/LINGUAS`.
- `Name[es]=` / `GenericName[es]=` / `Comment[es]=` / `Keywords[es]=` entries
  in the `.desktop` file.
- `tests/test_i18n.py` regression guards: AST walker that fails the build
  if `_()` is called at module or class scope, plus a parametrized check
  that every `ErrorCode` has a registered formatter.
- "Translations" section in `CONTRIBUTING.md` with the workflow and the
  `_()` / `N_()` discipline.
- `babel >= 2.14` as a dev dependency; `locale/` shipped in the wheel via
  `force-include`.

### Changed

- Page identity is now decoupled from displayed labels: each page exposes a
  stable `NAME: ClassVar[str]` (used by `Gtk.Stack` and sidebar lookups) so
  translating `TITLE` no longer risks breaking navigation.
- Sidebar group identity is modelled as a `PageGroup` enum; comparisons are
  identity-based, labels are wrapped with `N_()` and resolved to the active
  locale at render time.
- Service-layer error classes (`ConnectionsError`, `CompositeTemplateError`,
  `FileFormatError`, `ExecutionError`, `LoopbackError`, `OAuthError`,
  `ApiError`) now inherit from a shared `CodedError` base. Existing
  positional-message constructors keep working — `code=` and `params=` are
  optional kwargs added at user-facing raise sites.

## [0.1.0a1] - 2026-05-01

### Added

- Initial project scaffolding: GTK4 + libadwaita application skeleton with a
  placeholder *Welcome* page.
- Cross-platform configuration storage via `platformdirs` (no GSettings).
- Cross-platform credential storage wrapper around `keyring` (Secret Service
  on Linux, Keychain on macOS, Credential Manager on Windows). Shape ready for
  Salesforce OAuth 2.0 PKCE tokens.
- `AppState` model with live / saved / default per-field tracking and an
  `is_dirty` property to support the upcoming Composite API form.
- `make_page_layout()` helper and `Timer` debounced-timeout helper, ported
  from Hyprmod.
- Linux-only GitHub Actions CI: ruff (check + format), pyright strict, pytest
  on Python 3.12 and 3.13. macOS / Windows CI deferred until users land on
  those platforms.
- MIT license, README with per-OS install instructions, CONTRIBUTING with the
  four-check workflow.
