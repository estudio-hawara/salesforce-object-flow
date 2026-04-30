"""Generic JSON-on-disk cache.

Used by :mod:`services.sobjects` to avoid refetching SObject metadata on
every page load. Keys are tuples that hash to a stable filename; values are
JSON-serialisable mappings. No TTL — invalidation is explicit (manual
"Refresh" button or schema-version bump).

Lives under ``platformdirs.user_cache_dir`` so OS-level cache cleaners can
clear it without breaking app state. Atomic writes via tmp + ``replace``,
matching :class:`Config.save`.

Each on-disk entry is wrapped in an envelope::

    {"__meta__": {"instance_url": ..., "api_version": ..., "extra": ...},
     "payload": <caller's mapping>}

so :meth:`JsonCache.delete_namespace` can scan a namespace and drop only the
files belonging to the given ``(instance_url, api_version)`` pair.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from platformdirs import PlatformDirs

log = logging.getLogger(__name__)

# Bumping this directory name (e.g. v1 → v2) is the no-migration escape
# hatch when the on-disk shape ever changes incompatibly.
_SCHEMA_VERSION = "v1"
_META_KEY = "__meta__"
_PAYLOAD_KEY = "payload"

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=False)


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Stable identifier for a cache entry.

    ``instance_url`` is preferred over the user's alias because aliases can
    be renamed and the same Salesforce org may eventually be referenced
    under multiple labels (e.g. scratch-org workflows). ``api_version`` is
    part of the key so a version bump invalidates without manual cleanup.
    """

    namespace: str
    instance_url: str
    api_version: str
    extra: str = ""


class JsonCache:
    """Per-app JSON-on-disk cache. All operations are best-effort: failures
    are logged at ``WARNING`` and surfaced as cache misses, never raised.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _default_root()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: CacheKey) -> Path:
        digest = hashlib.sha1(
            f"{key.instance_url}|{key.api_version}|{key.extra}".encode("utf-8")
        ).hexdigest()[:16]
        return self._root / f"{key.namespace}__{digest}.json"

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        path = self.path_for(key)
        envelope = _read_envelope(path)
        if envelope is None:
            return None
        payload = envelope.get(_PAYLOAD_KEY)
        if not isinstance(payload, dict):
            log.warning("Cache entry at %s has no payload; deleting", path)
            _safe_unlink(path)
            return None
        return cast(dict[str, Any], payload)

    def set(self, key: CacheKey, value: Mapping[str, Any]) -> None:
        path = self.path_for(key)
        envelope = {
            _META_KEY: {
                "instance_url": key.instance_url,
                "api_version": key.api_version,
                "extra": key.extra,
            },
            _PAYLOAD_KEY: dict(value),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("Cache write failed at %s: %s", path, exc)

    def delete(self, key: CacheKey) -> None:
        _safe_unlink(self.path_for(key))

    def delete_namespace(self, namespace: str, instance_url: str, api_version: str) -> int:
        """Delete every entry in *namespace* whose meta matches the given
        ``(instance_url, api_version)``. Returns the count removed.
        """
        if not self._root.exists():
            return 0
        prefix = f"{namespace}__"
        removed = 0
        for entry in self._root.iterdir():
            if not entry.is_file() or not entry.name.startswith(prefix):
                continue
            envelope = _read_envelope(entry)
            if envelope is None:
                continue
            meta = envelope.get(_META_KEY)
            if not isinstance(meta, dict):
                continue
            meta_dict = cast(dict[str, Any], meta)
            if (
                meta_dict.get("instance_url") == instance_url
                and meta_dict.get("api_version") == api_version
            ):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def default_cache() -> JsonCache:
    """Return a cache rooted at the platform-default user cache directory."""
    return JsonCache(_default_root())


def _default_root() -> Path:
    return Path(_DIRS.user_cache_dir) / _SCHEMA_VERSION


def _read_envelope(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cache read failed at %s; treating as miss (%s)", path, exc)
        _safe_unlink(path)
        return None
    if not isinstance(data, dict):
        log.warning("Cache entry at %s is not a JSON object; deleting", path)
        _safe_unlink(path)
        return None
    return cast(dict[str, Any], data)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
