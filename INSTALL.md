# Install

Salesforce Object Flow is cross-platform. The polished daily-driver target is **Linux**; macOS and Windows are supported via Homebrew and MSYS2 respectively and may exhibit minor libadwaita theming quirks.

## Linux (Debian / Ubuntu)

```bash
# Install the system dependencies
sudo apt install libcairo2-dev libgirepository-2.0-dev libgtk-4-dev libadwaita-1-dev

# Clone the repository
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow

# Install the aplication dependencies
uv sync

# Run the application
uv run salesforce-object-flow
```

On Fedora / Arch / openSUSE, install the equivalent `gtk4`, `libadwaita`,
`gobject-introspection`, and `cairo` development packages.

## macOS

```bash
# Install the system dependencies
brew install gtk4 libadwaita gobject-introspection pygobject3

# Clone the repository
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow

# Install the aplication dependencies
uv sync

# Run the application
uv run salesforce-object-flow
```

## Windows

Install [MSYS2](https://www.msys2.org/) and open the **UCRT64** shell:

```bash
# Install the system dependencies
pacman -S mingw-w64-ucrt-x86_64-gtk4 \
          mingw-w64-ucrt-x86_64-libadwaita \
          mingw-w64-ucrt-x86_64-python \
          mingw-w64-ucrt-x86_64-python-gobject \
          mingw-w64-ucrt-x86_64-python-pip \
          git

# Clone the repository
git clone https://github.com/estudio-hawara/salesforce-object-flow.git
cd salesforce-object-flow

# Install the aplication dependencies
python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install httpx keyring platformdirs pygobject-stubs
pip install --no-deps -e .

# Run the application
salesforce-object-flow
```

uv is not used on Windows: the MSYS2 Python reports its platform as `mingw_x86_64_ucrt_gnu`, which uv does not recognize. The pure-Python deps are installed explicitly and the project itself is installed with `--no-deps` so pip does not try to rebuild PyGObject (and its pycairo / gobject-introspection build chain) from PyPI — the venv reuses the PyGObject that `pacman` already installed, linked against the MSYS2-shipped GTK4 DLLs.

The app must be launched from the UCRT64 shell so that GTK4 typelibs and libadwaita are visible to PyGObject.