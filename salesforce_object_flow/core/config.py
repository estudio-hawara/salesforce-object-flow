"""Cross-platform JSON-backed application configuration.

Uses ``platformdirs`` for the on-disk location so the same code works on
Linux (``$XDG_CONFIG_HOME``), macOS (``~/Library/Application Support``), and
Windows (``%APPDATA%``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, cast

from platformdirs import PlatformDirs

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v63.0"

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=True)
_CONFIG_PATH = Path(_DIRS.user_config_dir) / "config.json"


@dataclass(slots=True)
class OrgEntry:
    """One Salesforce org the user has connected to.

    ``instance_url`` is what Salesforce returns from the OAuth token endpoint
    and is the canonical base for REST calls. ``my_domain_url`` is whatever
    the user typed in the Add-Org form; we keep it so re-auth flows can
    rebuild the authorize URL without depending on what SF echoed back.
    """

    alias: str
    instance_url: str
    my_domain_url: str
    client_id: str
    is_sandbox: bool = False
    api_version: str = DEFAULT_API_VERSION

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OrgEntry | None:
        """Build an entry from a JSON-decoded mapping; return ``None`` if malformed."""
        try:
            return cls(
                alias=str(data["alias"]),
                instance_url=str(data["instance_url"]),
                my_domain_url=str(data["my_domain_url"]),
                client_id=str(data["client_id"]),
                is_sandbox=bool(data.get("is_sandbox", False)),
                api_version=str(data.get("api_version", DEFAULT_API_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed org entry: %r", data)
            return None


@dataclass(slots=True)
class Config:
    """User-level application configuration."""

    auto_save: bool = False
    last_org_alias: str | None = None
    active_org_alias: str | None = None
    orgs: list[OrgEntry] = field(default_factory=list[OrgEntry])

    def find_org(self, alias: str) -> OrgEntry | None:
        for org in self.orgs:
            if org.alias == alias:
                return org
        return None

    def upsert_org(self, entry: OrgEntry) -> None:
        """Replace an existing entry by alias, or append if absent."""
        for i, existing in enumerate(self.orgs):
            if existing.alias == entry.alias:
                self.orgs[i] = entry
                return
        self.orgs.append(entry)

    def remove_org(self, alias: str) -> bool:
        """Remove the entry with *alias*. Clears ``active_org_alias`` if matched.

        Returns ``True`` if an entry was removed.
        """
        for i, existing in enumerate(self.orgs):
            if existing.alias == alias:
                del self.orgs[i]
                if self.active_org_alias == alias:
                    self.active_org_alias = None
                return True
        return False

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
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

        typed_data = cast(dict[str, Any], data)
        # Drop unknown keys so older / newer schemas don't crash each other.
        scalar_field_names = {f.name for f in fields(cls) if f.name != "orgs"}
        filtered: dict[str, Any] = {k: v for k, v in typed_data.items() if k in scalar_field_names}
        config = cls(**filtered)

        raw_orgs = typed_data.get("orgs", [])
        if isinstance(raw_orgs, list):
            for item in cast(list[Any], raw_orgs):
                if isinstance(item, dict):
                    entry = OrgEntry.from_dict(cast(Mapping[str, Any], item))
                    if entry is not None:
                        config.orgs.append(entry)

        # Migrate from the deprecated last_org_alias if active is not set.
        if config.active_org_alias is None and config.last_org_alias is not None:
            config.active_org_alias = config.last_org_alias

        return config

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
