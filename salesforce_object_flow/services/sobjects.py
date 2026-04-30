"""SObject metadata orchestrator.

Read-only helpers for the Object Explorer page. Calls ``/sobjects`` and
``/sobjects/<Name>/describe`` via the active org's :class:`SalesforceClient`,
and caches the JSON envelopes via :class:`JsonCache` so the user pays for the
HTTP round-trip only on first access (or after an explicit refresh).

Synchronous; the page is responsible for offloading to a worker thread.
GTK-free.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from salesforce_object_flow.core.cache import CacheKey, JsonCache
from salesforce_object_flow.core.config import OrgEntry
from salesforce_object_flow.services.connections import ConnectionsError, ConnectionsService

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SObjectSummary:
    """One row from the ``/services/data/<v>/sobjects`` listing."""

    name: str
    label: str
    label_plural: str
    custom: bool
    queryable: bool
    createable: bool
    updateable: bool
    deletable: bool
    key_prefix: str | None


@dataclass(frozen=True, slots=True)
class PicklistValue:
    value: str
    label: str
    active: bool
    default_value: bool


@dataclass(frozen=True, slots=True)
class SObjectField:
    name: str
    label: str
    type: str
    length: int | None
    nillable: bool
    createable: bool
    updateable: bool
    unique: bool
    external_id: bool
    picklist_values: tuple[PicklistValue, ...]
    reference_to: tuple[str, ...]
    relationship_name: str | None


@dataclass(frozen=True, slots=True)
class SObjectDescribe:
    name: str
    label: str
    custom: bool
    fields: tuple[SObjectField, ...]


class SObjectService:
    """Cache-backed reads for SObject metadata.

    Owns no GTK; takes a :class:`ConnectionsService` for client construction
    and a :class:`JsonCache` for persistence.
    """

    LIST_NS: ClassVar[str] = "sobjects.list"
    DESCRIBE_NS: ClassVar[str] = "sobjects.describe"

    def __init__(self, connections: ConnectionsService, cache: JsonCache) -> None:
        self._connections = connections
        self._cache = cache

    # --------------------------------------------------------------- List
    def list_sobjects(self, alias: str) -> list[SObjectSummary]:
        entry = self._entry_for(alias)
        key = self._key_list(entry)
        cached = self._cache.get(key)
        if cached is not None:
            return _parse_summaries(cached)
        return self._fetch_list(alias, entry, key)

    def refresh_list(self, alias: str) -> list[SObjectSummary]:
        entry = self._entry_for(alias)
        key = self._key_list(entry)
        self._cache.delete(key)
        return self._fetch_list(alias, entry, key)

    def _fetch_list(self, alias: str, entry: OrgEntry, key: CacheKey) -> list[SObjectSummary]:
        path = f"/services/data/{entry.api_version}/sobjects"
        with self._connections.get_authenticated_client(alias) as client:
            payload = client.get(path)
        if not isinstance(payload, dict):
            raise ConnectionsError("Unexpected /sobjects response shape")
        envelope = cast(dict[str, Any], payload)
        self._cache.set(key, envelope)
        return _parse_summaries(envelope)

    # ----------------------------------------------------------- Describe
    def describe(self, alias: str, sobject_name: str) -> SObjectDescribe:
        entry = self._entry_for(alias)
        key = self._key_describe(entry, sobject_name)
        cached = self._cache.get(key)
        if cached is not None:
            return _parse_describe(cached)

        path = f"/services/data/{entry.api_version}/sobjects/{sobject_name}/describe"
        with self._connections.get_authenticated_client(alias) as client:
            payload = client.get(path)
        if not isinstance(payload, dict):
            raise ConnectionsError(f"Unexpected /sobjects/{sobject_name}/describe response shape")
        envelope = cast(dict[str, Any], payload)
        self._cache.set(key, envelope)
        return _parse_describe(envelope)

    def invalidate_describe(self, alias: str, sobject_name: str) -> None:
        entry = self._entry_for(alias)
        self._cache.delete(self._key_describe(entry, sobject_name))

    # ---------------------------------------------------------------- Util
    def _entry_for(self, alias: str) -> OrgEntry:
        for entry in self._connections.list_orgs():
            if entry.alias == alias:
                return entry
        raise ConnectionsError(f"No org with alias '{alias}' is registered.")

    def _key_list(self, entry: OrgEntry) -> CacheKey:
        return CacheKey(
            namespace=self.LIST_NS,
            instance_url=entry.instance_url,
            api_version=entry.api_version,
        )

    def _key_describe(self, entry: OrgEntry, sobject_name: str) -> CacheKey:
        return CacheKey(
            namespace=self.DESCRIBE_NS,
            instance_url=entry.instance_url,
            api_version=entry.api_version,
            extra=sobject_name,
        )


# ====================================================================
# Salesforce JSON → dataclass parsers
# ====================================================================


def _parse_summaries(envelope: Mapping[str, Any]) -> list[SObjectSummary]:
    raw = envelope.get("sobjects")
    if not isinstance(raw, list):
        return []
    summaries: list[SObjectSummary] = []
    for item in cast(list[Any], raw):
        if not isinstance(item, dict):
            continue
        summary = _summary_from_dict(cast(dict[str, Any], item))
        if summary is not None:
            summaries.append(summary)
    return summaries


def _summary_from_dict(data: Mapping[str, Any]) -> SObjectSummary | None:
    name = data.get("name")
    if not isinstance(name, str):
        return None
    label = _str(data.get("label"), name)
    return SObjectSummary(
        name=name,
        label=label,
        label_plural=_str(data.get("labelPlural"), label),
        custom=bool(data.get("custom", False)),
        queryable=bool(data.get("queryable", False)),
        createable=bool(data.get("createable", False)),
        updateable=bool(data.get("updateable", False)),
        deletable=bool(data.get("deletable", False)),
        key_prefix=_optional_str(data.get("keyPrefix")),
    )


def _parse_describe(envelope: Mapping[str, Any]) -> SObjectDescribe:
    name = _str(envelope.get("name"), "")
    label = _str(envelope.get("label"), name)
    fields_raw = envelope.get("fields")
    fields: list[SObjectField] = []
    if isinstance(fields_raw, list):
        for raw in cast(list[Any], fields_raw):
            if isinstance(raw, dict):
                field = _field_from_dict(cast(dict[str, Any], raw))
                if field is not None:
                    fields.append(field)
    return SObjectDescribe(
        name=name,
        label=label,
        custom=bool(envelope.get("custom", False)),
        fields=tuple(fields),
    )


def _field_from_dict(data: Mapping[str, Any]) -> SObjectField | None:
    name = data.get("name")
    if not isinstance(name, str):
        return None
    picklist_raw = data.get("picklistValues")
    picklist_values: list[PicklistValue] = []
    if isinstance(picklist_raw, list):
        for raw in cast(list[Any], picklist_raw):
            if isinstance(raw, dict):
                value = _picklist_from_dict(cast(dict[str, Any], raw))
                if value is not None:
                    picklist_values.append(value)
    reference_raw = data.get("referenceTo")
    reference_to: tuple[str, ...] = ()
    if isinstance(reference_raw, list):
        reference_to = tuple(
            item for item in cast(list[Any], reference_raw) if isinstance(item, str)
        )
    return SObjectField(
        name=name,
        label=_str(data.get("label"), name),
        type=_str(data.get("type"), "unknown"),
        length=_optional_int(data.get("length")),
        nillable=bool(data.get("nillable", False)),
        createable=bool(data.get("createable", False)),
        updateable=bool(data.get("updateable", False)),
        unique=bool(data.get("unique", False)),
        external_id=bool(data.get("externalId", False)),
        picklist_values=tuple(picklist_values),
        reference_to=reference_to,
        relationship_name=_optional_str(data.get("relationshipName")),
    )


def _picklist_from_dict(data: Mapping[str, Any]) -> PicklistValue | None:
    value = data.get("value")
    if not isinstance(value, str):
        return None
    return PicklistValue(
        value=value,
        label=_str(data.get("label"), value),
        active=bool(data.get("active", True)),
        default_value=bool(data.get("defaultValue", False)),
    )


def _str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
