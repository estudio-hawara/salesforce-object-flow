# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
