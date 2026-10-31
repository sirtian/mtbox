"""Cross-restart rate limiting and API budget accounting.

All calls to the M-Team API go through :class:`ApiLimiter`. The limiter keeps
a small in-memory interval guard and persists every call in ``api_call`` so a
restart cannot silently reset the daily budget.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from ..config import Settings
from ..db import Database
from ..errors import BudgetExceeded, CooldownActive, RateLimitBusy
from ..timeutil import hours_ago_iso, parse_dt, to_iso, utc_now
from . import settings_store

ENDPOINTS = ("search", "torrent", "profile")
COOLDOWN_KEY = "cooldown_until"


@dataclass(frozen=True, slots=True)
class Usage:
    search: int
    torrent: int
    profile: int

    def as_dict(self) -> dict[str, int]:
        return {"search": self.search, "torrent": self.torrent, "profile": self.profile}


class ApiLimiter:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._locks = {endpoint: threading.Lock() for endpoint in ENDPOINTS}
        self._last_call: dict[str, float] = {}
        self._last_row: dict[str, int] = {}
        # Search budget can be overridden from the settings page; read it per
        # call so the change applies without a restart. Torrent/profile budgets
        # remain environment-only.
        self._budgets = {
            "torrent": settings.mt_download_daily_budget,
            "profile": settings.mt_profile_daily_budget,
        }

    # ---------- public ----------

    def acquire(self, endpoint: str, *, wait: bool = True) -> None:
        if endpoint not in ENDPOINTS:
            raise ValueError(f"未知端点: {endpoint}")

        with self._locks[endpoint]:
            retry_after = self._cooldown_remaining()
            if retry_after > 0:
                raise CooldownActive(retry_after, self._cooldown_reason())

            used = self._count_recent(endpoint)
            budget = self._budget(endpoint)
            if used >= budget:
                raise BudgetExceeded(endpoint, budget, used)

            remaining = self._interval_remaining(endpoint)
            if remaining > 0:
                if not wait:
                    raise RateLimitBusy(int(math.ceil(remaining)))
                time.sleep(remaining)
                # A cooldown may have been set while we slept.
                retry_after = self._cooldown_remaining()
                if retry_after > 0:
                    raise CooldownActive(retry_after, self._cooldown_reason())

            with self.db.tx() as conn:
                cursor = conn.execute(
                    "INSERT INTO api_call(endpoint, called_at, ok) VALUES (?, ?, 1)",
                    (endpoint, to_iso(utc_now())),
                )
                self._last_row[endpoint] = int(cursor.lastrowid)
            self._last_call[endpoint] = time.time()

    def record(self, endpoint: str, ok: bool) -> None:
        row_id = self._last_row.get(endpoint)
        if not row_id:
            return
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE api_call SET ok = ? WHERE id = ?",
                (1 if ok else 0, row_id),
            )

    def usage(self) -> Usage:
        return Usage(
            search=self._count_recent("search"),
            torrent=self._count_recent("torrent"),
            profile=self._count_recent("profile"),
        )

    def budgets(self) -> dict[str, int]:
        return {
            "search": self._budget("search"),
            "torrent": self._budgets["torrent"],
            "profile": self._budgets["profile"],
        }

    def _budget(self, endpoint: str) -> int:
        if endpoint == "search":
            return settings_store.effective_search_budget(
                self.db, self.settings.mt_search_daily_budget
            )
        return self._budgets[endpoint]

    def set_cooldown(self, seconds: int, reason: str = "") -> None:
        until = to_iso(utc_now().replace(microsecond=0) + timedelta(seconds=seconds))
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO setting(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (COOLDOWN_KEY, until),
            )
            if reason:
                conn.execute(
                    "INSERT INTO setting(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("cooldown_reason", reason),
                )

    def clear_cooldown(self) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM setting WHERE key IN (?, ?)", (COOLDOWN_KEY, "cooldown_reason"))

    def cooldown_remaining(self) -> int:
        return self._cooldown_remaining()

    def _cooldown_reason(self) -> str:
        with self.db.read() as conn:
            row = conn.execute("SELECT value FROM setting WHERE key = 'cooldown_reason'").fetchone()
        return str(row["value"]) if row else ""

    def _cooldown_remaining(self) -> int:
        with self.db.read() as conn:
            row = conn.execute("SELECT value FROM setting WHERE key = ?", (COOLDOWN_KEY,)).fetchone()
        if not row:
            return 0
        until = parse_dt(row["value"])
        if until is None:
            return 0
        seconds = int((until - utc_now()).total_seconds())
        return max(0, seconds)

    def _interval_remaining(self, endpoint: str) -> float:
        if endpoint != "search":
            return 0.0
        # The settings page can disable or change the interval without a
        # restart; the .env value remains the fallback.
        min_interval = settings_store.effective_search_min_interval(
            self.db, self.settings.mt_search_min_interval_sec
        )
        if min_interval <= 0:
            return 0.0
        last_ts = self._last_call.get(endpoint)
        if last_ts is None:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT called_at FROM api_call WHERE endpoint = ? "
                    "ORDER BY called_at DESC LIMIT 1",
                    (endpoint,),
                ).fetchone()
            if row:
                parsed = parse_dt(row["called_at"])
                if parsed is not None:
                    last_ts = parsed.timestamp()
        if last_ts is None:
            return 0.0
        return max(0.0, min_interval - (time.time() - last_ts))

    def _count_recent(self, endpoint: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM api_call WHERE endpoint = ? AND called_at >= ?",
                (endpoint, hours_ago_iso(24)),
            ).fetchone()
        return int(row["n"])

    def clear_accounting_before(self, hours: float) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM api_call WHERE called_at < ?", (hours_ago_iso(hours),))
