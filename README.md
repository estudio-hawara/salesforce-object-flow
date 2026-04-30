# Salesforce Object Flow

A native GTK4/libadwaita desktop GUI for Salesforce — compose multi-object,
transactional creates against the Salesforce REST **Composite API** without
leaving your desktop.

[![CI](https://github.com/estudio-hawara/salesforce-object-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/estudio-hawara/salesforce-object-flow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

## What it is

Salesforce Object Flow lets you assemble a single transactional Composite API
call that creates objects across multiple related tables (Account + Contact +
custom objects, etc.) in one round-trip — all-or-nothing, with per-field
validation and clear error reporting on the rare partial-success edge case.

## Status

`0.0.1` — project scaffolding only. The Composite API form lands in a follow-up
release. The current build opens a libadwaita window with a placeholder
*Welcome* page so you can confirm the GTK stack is wired up correctly on your
machine.

## Install

Salesforce Object Flow is cross-platform. The polished daily-driver target is
**Linux**; macOS and Windows are supported via Homebrew and MSYS2 respectively
and may exhibit minor libadwaita theming quirks.

### Linux (Debian / Ubuntu names)

```bash
sudo apt install libcairo2-dev libgirepository-2.0-dev libgtk-4-dev libadwaita-1-dev
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow
uv sync
uv run salesforce-object-flow
```

On Fedora / Arch / openSUSE, install the equivalent `gtk4`, `libadwaita`,
`gobject-introspection`, and `cairo` development packages.

### macOS

```bash
brew install gtk4 libadwaita gobject-introspection pygobject3
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow
uv sync
uv run salesforce-object-flow
```

### Windows

Install [MSYS2](https://www.msys2.org/) and open the **UCRT64** shell:

```bash
pacman -S mingw-w64-ucrt-x86_64-gtk4 \
          mingw-w64-ucrt-x86_64-libadwaita \
          mingw-w64-ucrt-x86_64-python \
          mingw-w64-ucrt-x86_64-python-gobject \
          mingw-w64-ucrt-x86_64-python-pip
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow
uv sync
uv run salesforce-object-flow
```

The app must be launched from the UCRT64 shell so that GTK4 typelibs and
libadwaita are visible to PyGObject.

## Development

```bash
uv sync
uv run salesforce-object-flow

uv run pytest tests/ -v
uv run ruff check salesforce_object_flow/ tests/
uv run ruff format --check salesforce_object_flow/ tests/
uv run pyright salesforce_object_flow/ tests/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## License

MIT — see [LICENSE](LICENSE).
