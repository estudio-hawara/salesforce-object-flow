"""Cross-platform credential storage for Salesforce orgs.

Backed by ``keyring``: Secret Service on Linux, Keychain on macOS, Credential
Manager on Windows. Tokens are stored per *org alias* so a user can keep
multiple orgs (production, sandbox, scratch) side-by-side.

Shape ready for OAuth 2.0 PKCE: each org alias holds an ``access_token``, a
``refresh_token``, and the ``instance_url`` returned by Salesforce. They live
under distinct keyring entries so each can be rotated independently.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE_NAME = "es.hawara.SalesforceObjectFlow"


class CredentialsError(RuntimeError):
    """Raised when the keyring backend is unusable."""


@dataclass(slots=True)
class OrgCredentials:
    """OAuth 2.0 PKCE token bundle for a single Salesforce org."""

    instance_url: str
    access_token: str
    refresh_token: str | None = None


def _key(org_alias: str) -> str:
    return f"org::{org_alias}"


def get(org_alias: str) -> OrgCredentials | None:
    """Return the stored credentials for *org_alias*, or ``None`` if absent."""
    try:
        raw = keyring.get_password(SERVICE_NAME, _key(org_alias))
    except keyring.errors.KeyringError as exc:
        raise CredentialsError(
            "Keyring backend is unavailable. On headless Linux, install and "
            "configure a Secret Service implementation (e.g. gnome-keyring or "
            "KeePassXC) before logging in to a Salesforce org."
        ) from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Corrupt keyring entry for %s; ignoring", org_alias)
        return None
    return OrgCredentials(
        instance_url=data["instance_url"],
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
    )


def set(org_alias: str, creds: OrgCredentials) -> None:
    """Store *creds* under *org_alias*, replacing any previous entry."""
    try:
        keyring.set_password(SERVICE_NAME, _key(org_alias), json.dumps(asdict(creds)))
    except keyring.errors.KeyringError as exc:
        raise CredentialsError("Failed to write credentials to the system keyring.") from exc


def delete(org_alias: str) -> None:
    """Remove the stored credentials for *org_alias*. No-op if absent."""
    try:
        keyring.delete_password(SERVICE_NAME, _key(org_alias))
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as exc:
        raise CredentialsError("Failed to delete credentials from the system keyring.") from exc
