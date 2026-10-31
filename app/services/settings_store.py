"""Runtime-tunable settings stored in the ``setting`` table."""

from __future__ import annotations

import json
from typing import Any

from ..db import Database

DEFAULTS: dict[str, str] = {
    "default_interval_min": "30",
    "auto_grab_default": "false",
    "min_score": "45",
    "max_candidates": "20",
    "candidate_ttl_days": "7",
    "season_pack_min_missing": "3",
    "max_grabs_per_check": "6",
    "new_episode_window_days": "7",
    "min_seed_hours": "72",
    "max_seed_hours": "336",
    "min_ratio": "1.0",
    "daily_download_budget_gb": "50",
    "ratio_pause_nonfree": "0.8",
    "ratio_pause_all": "0.6",
    "disk_pause_percent": "90",
    "movie_path_template": (
        "{library}/Movies/{title} ({year})/{title} ({year}) - {resolution}.{ext}"
    ),
    "tv_path_template": (
        "{library}/TV/{title} ({year})/Season {season:02d}/"
        "{title} - S{season:02d}E{episode:02d}.{ext}"
    ),
    "notify_config": "{}",
    "category_map": "{}",
    "mode_map": '{"movie": "normal", "tv": "tvshow"}',
}

_TRUTHY = {"1", "true", "yes", "on"}

# Not seeded by ``ensure_defaults``: the .env value stays the fallback until the
# user saves an override on the settings page.
SEARCH_DAILY_BUDGET_KEY = "search_daily_budget"


def ensure_defaults(db: Database) -> None:
    with db.tx() as conn:
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO setting(key, value) VALUES (?, ?)",
                (key, value),
            )


def get_value(db: Database, key: str) -> str | None:
    with db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def get_int(db: Database, key: str, default: int = 0) -> int:
    raw = get_value(db, key)
    if raw is None:
        raw = DEFAULTS.get(key)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def get_float(db: Database, key: str, default: float = 0.0) -> float:
    raw = get_value(db, key)
    if raw is None:
        raw = DEFAULTS.get(key)
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def get_bool(db: Database, key: str, default: bool = False) -> bool:
    raw = get_value(db, key)
    if raw is None:
        raw = DEFAULTS.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUTHY


def get_json(db: Database, key: str, default: Any = None) -> Any:
    raw = get_value(db, key)
    if raw is None:
        raw = DEFAULTS.get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def effective_search_budget(db: Database, default: int) -> int:
    """Return the settings-page override, else the .env/default budget."""
    return get_int(db, SEARCH_DAILY_BUDGET_KEY, default)


def set_values(db: Database, values: dict[str, str]) -> None:
    with db.tx() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO setting(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def delete_values(db: Database, keys: list[str]) -> None:
    if not keys:
        return
    with db.tx() as conn:
        for key in keys:
            conn.execute("DELETE FROM setting WHERE key = ?", (key,))


def all_values(db: Database) -> dict[str, str]:
    ensure_defaults(db)
    with db.read() as conn:
        rows = conn.execute("SELECT key, value FROM setting").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}
