"""Internationalization (i18n) plumbing.

Translation discipline:
- ``_(msg)`` translates at the call site. Safe inside functions/methods.
- ``N_(msg)`` is a no-op marker for strings evaluated at import time
  (module-level constants, ``ClassVar``, ``Enum`` values). Wrap with
  ``_()`` only when the value is read.

xgettext/pybabel extract both ``_`` and ``N_`` (configured via
``-k _ -k N_`` in ``scripts/i18n.py``).
"""

from __future__ import annotations

import gettext
import locale
from pathlib import Path

TEXTDOMAIN = "salesforce-object-flow"

_translation: gettext.NullTranslations = gettext.NullTranslations()


def _gettext(msg: str) -> str:
    return _translation.gettext(msg)


def _ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def N_(msg: str) -> str:
    """No-op marker for translatable strings evaluated at import time."""
    return msg


_ = _gettext
ngettext = _ngettext


def _find_locale_dir() -> Path | None:
    """Pick the first existing locale dir.

    Mirrors ``main._register_icons``: try the installed-wheel layout
    (``<package>/_locale``) first, then fall back to the editable repo
    layout (``<repo>/locale``).
    """
    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "_locale",
        package_root.parent / "locale",
    )
    for path in candidates:
        if path.is_dir():
            return path
    return None


def init() -> None:
    """Bind the translation catalog. Idempotent.

    Honours ``LANGUAGE`` env var via ``gettext.translation`` defaults.
    Falls back to source strings if no catalog is present.
    """
    global _translation
    locale_dir = _find_locale_dir()
    if locale_dir is None:
        _translation = gettext.NullTranslations()
        return

    _translation = gettext.translation(
        TEXTDOMAIN, localedir=str(locale_dir), fallback=True
    )

    # libintl wiring: covers strings GTK/GLib emit themselves
    # (default dialog buttons, future .ui/Blueprint files). Best-effort —
    # libintl bindings are not present on every platform.
    try:
        locale.bindtextdomain(TEXTDOMAIN, str(locale_dir))
        locale.textdomain(TEXTDOMAIN)
    except (AttributeError, OSError):
        pass
