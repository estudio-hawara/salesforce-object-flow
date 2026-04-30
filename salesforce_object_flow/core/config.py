"""Cross-platform JSON-backed application configuration.

Uses ``platformdirs`` for the on-disk location so the same code works on
Linux (``$XDG_CONFIG_HOME``), macOS (``~/Library/Application Support``), and
Windows (``%APPDATA%``).
"""

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast

from platformdirs import PlatformDirs

log = logging.getLogger(__name__)

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=True)
_CONFIG_PATH = Path(_DIRS.user_config_dir) / "config.json"


@dataclass(slots=True)
class Config:
    """User-level application configuration."""

    auto_save: bool = False
    last_org_alias: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load configuration from *path*; return defaults if missing or invalid."""
        path = path or _CONFIG_PATH
        if not path.exists():
            return cls()
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Failed to read %s; falling back to defaults", path)
            return cls()
        if not isinstance(data, dict):
            log.warning("Config at %s is not an object; falling back to defaults", path)
            return cls()
        # Drop unknown keys so older / newer schemas don't crash each other.
        typed_data = cast(dict[str, Any], data)
        known = {f.name for f in fields(cls)}
        filtered: dict[str, Any] = {k: v for k, v in typed_data.items() if k in known}
        return cls(**filtered)

    def save(self, path: Path | None = None) -> None:
        """Write configuration atomically to *path*."""
        path = path or _CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)


def config_path() -> Path:
    """Return the resolved path to the user's configuration file."""
    return _CONFIG_PATH
