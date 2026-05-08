# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
