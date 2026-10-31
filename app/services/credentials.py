"""Optional database overrides for MT API Key and qBittorrent password.

Environment variables remain the fallback and the documented default. Values
saved from ``/settings`` live in the local SQLite database so the user can
rotate a credential without recreating the container. They are never rendered
back to the browser or written to logs/events.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..db import Database
from . import settings_store

MT_API_KEY = "mt_api_key"
QB_PASSWORD = "qb_password"
SECRET_KEYS = (MT_API_KEY, QB_PASSWORD)
MAX_SECRET_LENGTH = 512


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    configured: bool
    source: str  # "settings", "env" or "none"


def env_values(settings: Settings) -> dict[str, str]:
    return {
        MT_API_KEY: settings.mt_api_key,
        QB_PASSWORD: settings.qb_password,
    }


def stored_values(db: Database) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = settings_store.get_value(db, key)
        if value:
            values[key] = value
    return values


def apply_overrides(settings: Settings, db: Database) -> dict[str, str]:
    """Apply stored credentials over the environment values and return them."""
    for key, value in stored_values(db).items():
        setattr(settings, key, value)
    return env_values(settings)


def status(settings: Settings, db: Database, key: str) -> CredentialStatus:
    if settings_store.get_value(db, key):
        return CredentialStatus(True, "settings")
    base = str(getattr(settings, key) or "")
    # A stored override was applied to ``settings`` at startup, so a non-empty
    # value with no database row necessarily comes from the environment.
    return CredentialStatus(bool(base), "env" if base else "none")


def save(db: Database, key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        raise KeyError(key)
    settings_store.set_values(db, {key: value})


def clear(db: Database, key: str) -> None:
    if key not in SECRET_KEYS:
        raise KeyError(key)
    settings_store.delete_values(db, [key])


def validate(value: str, key: str) -> str | None:
    """Return an error message, or None when the value is acceptable."""
    if not value:
        return None
    if len(value) > MAX_SECRET_LENGTH:
        return "凭据长度不能超过 512 个字符"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "凭据不能包含换行或控制字符"
    if key == MT_API_KEY and any(char.isspace() for char in value):
        return "MT API Key 不能包含空白字符"
    return None
