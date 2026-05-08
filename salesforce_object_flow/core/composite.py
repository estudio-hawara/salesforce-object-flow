"""Composite template definitions.

Pure data + JSON. GTK-free. A :class:`CompositeTemplate` describes a
Salesforce Composite REST API call: a list of subrequests, each with method,
URL, optional body and headers, plus top-level transactional flags. Stored on
disk as one JSON file per template under
``platformdirs.user_data_dir / composites/`` (handled by
:class:`services.composite.CompositeTemplateStore`).

Each template links to exactly one :class:`core.formats.FileFormat` by its
on-disk filename, so the editor can validate ``{{col}}`` placeholders against
the columns of that format.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, cast

log = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 1
MAX_SUBREQUESTS: Final[int] = 25
REFERENCE_ID_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class HttpMethod(StrEnum):
    """HTTP methods supported by Salesforce Composite subrequests."""

    GET = "GET"
    POST = "POST"
    PATCH = "PATCH"
    PUT = "PUT"
    DELETE = "DELETE"


METHODS_WITH_BODY: Final[frozenset[HttpMethod]] = frozenset(
    {HttpMethod.POST, HttpMethod.PATCH, HttpMethod.PUT}
)


@dataclass(slots=True)
class BodyField:
    """One key/value pair in a subrequest body.

    ``field`` is the Salesforce field name (e.g. ``Name``, ``Email``).
    ``value`` is either a literal string, a single ``{{col}}`` placeholder
    (gets typed substitution from the linked FileFormat at render time), or
    a string with embedded ``{{col}}`` / ``@{ref.path}`` tokens (always
    rendered as a string).
    """

    field: str
    value: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BodyField | None:
        try:
            field_name = str(data["field"]).strip()
            if not field_name:
                log.warning("Dropping body field with empty name")
                return None
            return cls(field=field_name, value=str(data.get("value", "")))
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed body field entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "value": self.value}


@dataclass(slots=True)
class Subrequest:
    """One step inside a :class:`CompositeTemplate`.

    ``body`` is ``None`` for methods that don't carry one (GET, DELETE) or
    when the user just hasn't added any fields yet. Otherwise it's a list
    of :class:`BodyField` entries that the renderer assembles into a JSON
    object at execution time.
    """

    reference_id: str
    method: HttpMethod
    url: str
    body: list[BodyField] | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Subrequest | None:
        try:
            reference_id = str(data["reference_id"]).strip()
            if not reference_id:
                log.warning("Dropping subrequest with empty reference_id")
                return None
            method_value = str(data["method"])
            try:
                method = HttpMethod(method_value)
            except ValueError:
                log.warning("Dropping subrequest with unknown method: %r", method_value)
                return None
            url = str(data["url"])
            raw_body = data.get("body", None)
            body: list[BodyField] | None
            if raw_body is None:
                body = None
            elif isinstance(raw_body, list):
                parsed: list[BodyField] = []
                for raw in cast(list[Any], raw_body):
                    if isinstance(raw, dict):
                        entry = BodyField.from_dict(cast(Mapping[str, Any], raw))
                        if entry is not None:
                            parsed.append(entry)
                body = parsed
            else:
                log.warning("Dropping subrequest with non-list body: %r", raw_body)
                return None
            raw_headers = data.get("headers", {})
            headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                for key, value in cast(dict[str, Any], raw_headers).items():
                    headers[str(key)] = str(value)
            return cls(
                reference_id=reference_id,
                method=method,
                url=url,
                body=body,
                headers=headers,
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed subrequest entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "method": self.method.value,
            "url": self.url,
            "body": ([entry.to_dict() for entry in self.body] if self.body is not None else None),
            "headers": dict(self.headers),
        }


@dataclass(slots=True)
class CompositeTemplate:
    """A Salesforce Composite payload template. Mutated in place by the editor."""

    name: str
    description: str = ""
    format_filename: str = ""
    all_or_none: bool = True
    collate_subrequests: bool = False
    subrequests: list[Subrequest] = field(default_factory=list[Subrequest])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompositeTemplate | None:
        try:
            name = str(data["name"]).strip()
            if not name:
                log.warning("Dropping composite template with empty name")
                return None
            raw_subs = data.get("subrequests", [])
            subrequests: list[Subrequest] = []
            if isinstance(raw_subs, list):
                for raw in cast(list[Any], raw_subs):
                    if isinstance(raw, dict):
                        sub = Subrequest.from_dict(cast(Mapping[str, Any], raw))
                        if sub is not None:
                            subrequests.append(sub)
            return cls(
                name=name,
                description=str(data.get("description", "")),
                format_filename=str(data.get("format_filename", "")),
                all_or_none=bool(data.get("all_or_none", True)),
                collate_subrequests=bool(data.get("collate_subrequests", False)),
                subrequests=subrequests,
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Dropping malformed composite template entry: %r", data)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "format_filename": self.format_filename,
            "all_or_none": self.all_or_none,
            "collate_subrequests": self.collate_subrequests,
            "subrequests": [sub.to_dict() for sub in self.subrequests],
        }
