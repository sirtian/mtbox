"""Subscription CRUD and episode progress."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ..db import Database
from ..errors import Conflict, NotFound
from ..models import Progress, Subscription
from ..timeutil import add_minutes_iso, parse_dt, to_iso, utc_now

KINDS = {"movie", "tv"}
STATUSES = {"active", "paused", "done"}

_EDITABLE_TEXT_FIELDS = (
    "title",
    "original_title",
    "douban_id",
    "imdb_id",
    "library_category",
)
CATEGORY_MAX_LEN = 40
_EDITABLE_INT_FIELDS = (
    "year",
    "season",
    "episode_offset",
    "episode_from",
    "episode_to",
    "interval_min",
)


def normalize_category(value: Any) -> str | None:
    """Trim and bound the library category used as a template/path segment."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:CATEGORY_MAX_LEN] or None


def _json(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def normalize_id(value: str | None, kind: str) -> str | None:
    """Keep only the numeric Douban id / tt IMDb id."""
    if not value:
        return None
    text = str(value).strip()
    if kind == "douban":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits or None
    if kind == "imdb":
        lowered = text.lower()
        index = lowered.find("tt")
        if index >= 0:
            candidate = text[index : index + 12]
            digits = "".join(ch for ch in candidate[2:] if ch.isdigit())
            return f"tt{digits}" if digits else None
        return text if text.startswith("tt") else None
    return text or None


class SubscriptionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- reads ----------

    def list(self, kind: str | None = None, status: str | None = None) -> list[Subscription]:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM subscription WHERE {' AND '.join(clauses)} ORDER BY id DESC",
                params,
            ).fetchall()
        return [Subscription.from_row(row) for row in rows]

    def get(self, sub_id: int) -> Subscription:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM subscription WHERE id = ? AND deleted_at IS NULL", (sub_id,)
            ).fetchone()
        if not row:
            raise NotFound(f"订阅 {sub_id} 不存在")
        return Subscription.from_row(row)

    def due(self, now: datetime | None = None) -> list[Subscription]:
        reference = now or utc_now()
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM subscription "
                "WHERE status = 'active' AND deleted_at IS NULL "
                "AND (last_checked_at IS NULL OR last_checked_at <= ?) "
                "ORDER BY last_checked_at IS NOT NULL, last_checked_at",
                (to_iso(reference),),
            ).fetchall()
        due: list[Subscription] = []
        for row in rows:
            sub = Subscription.from_row(row)
            if self._next_check_at(sub) <= reference:
                due.append(sub)
        return due

    @staticmethod
    def _next_check_at(sub: Subscription) -> datetime:
        if sub.last_checked_at is None:
            return datetime.min.replace(tzinfo=utc_now().tzinfo)
        last = parse_dt(sub.last_checked_at) or utc_now()
        delay = sub.interval_min * (2 ** min(max(sub.error_count, 0), 4))
        delay = min(delay, 24 * 60)
        return last + timedelta(minutes=delay)

    # ---------- writes ----------

    def create(self, data: dict[str, Any]) -> Subscription:
        kind = str(data.get("kind", "tv"))
        if kind not in KINDS:
            raise Conflict(f"未知订阅类型: {kind}")
        title = str(data.get("title", "")).strip()
        if not title:
            raise Conflict("订阅标题不能为空")
        season = int(data.get("season") or 1)
        if kind == "movie":
            season = 1
        now = utc_now()
        values = {
            "kind": kind,
            "title": title,
            "original_title": data.get("original_title") or None,
            "aliases": _json(data.get("aliases"), "[]"),
            "year": data.get("year"),
            "douban_id": normalize_id(data.get("douban_id"), "douban"),
            "imdb_id": normalize_id(data.get("imdb_id"), "imdb"),
            "library_category": normalize_category(data.get("library_category")),
            "season": season,
            "episode_offset": int(data.get("episode_offset") or 0),
            "episode_from": data.get("episode_from"),
            "episode_to": data.get("episode_to"),
            "quality": _json(data.get("quality"), "{}"),
            "rules": _json(data.get("rules"), "{}"),
            "auto_grab": 1 if data.get("auto_grab") else 0,
            "interval_min": max(1, int(data.get("interval_min") or 30)),
            "status": "active",
            "created_at": to_iso(now),
            "updated_at": to_iso(now),
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.db.tx() as conn:
            cursor = conn.execute(
                f"INSERT INTO subscription({columns}) VALUES ({placeholders})",
                list(values.values()),
            )
            sub_id = int(cursor.lastrowid)
        return self.get(sub_id)

    def update(self, sub_id: int, patch: dict[str, Any]) -> Subscription:
        current = self.get(sub_id)
        updates: dict[str, Any] = {}
        for key in _EDITABLE_TEXT_FIELDS:
            if key in patch:
                if key == "library_category":
                    updates[key] = normalize_category(patch[key])
                else:
                    updates[key] = patch[key] or None
        for key in _EDITABLE_INT_FIELDS:
            if key in patch:
                updates[key] = patch[key]
        if "aliases" in patch:
            updates["aliases"] = _json(patch["aliases"], "[]")
        if "quality" in patch:
            updates["quality"] = _json(patch["quality"], "{}")
        if "rules" in patch:
            updates["rules"] = _json(patch["rules"], "{}")
        if "auto_grab" in patch:
            updates["auto_grab"] = 1 if patch["auto_grab"] else 0
        if "status" in patch:
            if patch["status"] not in STATUSES:
                raise Conflict(f"未知订阅状态: {patch['status']}")
            updates["status"] = patch["status"]
        if "title" in updates and not updates["title"]:
            raise Conflict("订阅标题不能为空")
        if "kind" in patch and patch["kind"] != current.kind:
            raise Conflict("订阅类型不可修改，请新建订阅")
        if not updates:
            return current
        updates["updated_at"] = to_iso(utc_now())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE subscription SET {assignments} WHERE id = ? AND deleted_at IS NULL",
                [*updates.values(), sub_id],
            )
        return self.get(sub_id)

    def delete(self, sub_id: int) -> int:
        """Soft-delete a subscription and drop its candidate pool.

        Candidate/ignored rows have no download value once the subscription is
        gone, so they are deleted outright.  Real tasks stay in the database:
        the tasks page hides them through the subscription join, and seeding
        cleanup keeps managing organized torrents.  Returns the number of
        candidate rows removed.
        """
        self.get(sub_id)
        now = to_iso(utc_now())
        with self.db.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM torrent WHERE subscription_id = ? "
                "AND state IN ('candidate','ignored')",
                (sub_id,),
            )
            removed = int(cursor.rowcount or 0)
            conn.execute(
                "UPDATE subscription SET deleted_at = ?, status = 'paused', updated_at = ? "
                "WHERE id = ?",
                (now, now, sub_id),
            )
        return removed

    def mark_checked(
        self,
        sub_id: int,
        *,
        error: str | None = None,
        delay_min: int | None = None,
    ) -> None:
        sub = self.get(sub_id)
        now = utc_now()
        values: dict[str, Any] = {
            "last_checked_at": to_iso(now),
            "updated_at": to_iso(now),
            "last_error": error,
        }
        if error:
            values["error_count"] = sub.error_count + 1
        else:
            values["error_count"] = 0
        if delay_min is not None:
            # Record a future check time directly; due() adds interval to last_checked.
            values["last_checked_at"] = add_minutes_iso(now, delay_min - sub.interval_min)
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE subscription SET {assignments} WHERE id = ?",
                [*values.values(), sub_id],
            )

    # ---------- progress ----------

    def sync_episode_info(
        self,
        sub_id: int,
        *,
        total: int | None = None,
        available: int | None = None,
        source: str | None = None,
    ) -> Subscription:
        """Persist the episode total/latest episode discovered from PT/Douban.

        Counts only grow: a later search that is missing an older release must
        not make an already-known total look smaller.
        """
        sub = self.get(sub_id)
        updates: dict[str, Any] = {}
        for column, value in (("total_episodes", total), ("available_episode", available)):
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if not 0 < number <= 1000:
                continue
            current = getattr(sub, column)
            if current is None or number > current:
                updates[column] = number
        if source and (updates.get("total_episodes") is not None or sub.total_episodes is not None):
            updates["episodes_source"] = source
        if not updates:
            return sub
        updates["updated_at"] = to_iso(utc_now())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE subscription SET {assignments} WHERE id = ? AND deleted_at IS NULL",
                [*updates.values(), sub_id],
            )
        return self.get(sub_id)

    def progress(self, sub_id: int) -> Progress:
        sub = self.get(sub_id)
        scope = ""
        params: list[Any] = [sub_id]
        total: int | None = None
        lower: int | None = None
        if sub.kind == "tv":
            lower = sub.episode_from
            if lower is not None:
                upper = (
                    sub.episode_to
                    if sub.episode_to is not None
                    else (sub.total_episodes or lower)
                )
                if upper >= lower:
                    total = upper - lower + 1
                    scope = " AND episode BETWEEN ? AND ?"
                    params.extend([lower, upper])
            elif sub.total_episodes:
                total = sub.total_episodes

        movie_state: str | None = None
        with self.db.read() as conn:
            if sub.kind != "tv":
                # 电影没有分集记录，进度列按最近一次实际任务的状态展示，
                # 这样任务页整理完成后订阅页也能同步显示“已整理”。
                latest = conn.execute(
                    "SELECT state FROM torrent WHERE subscription_id = ? "
                    "AND state NOT IN ('candidate', 'ignored', 'deleted') "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (sub_id,),
                ).fetchone()
                movie_state = str(latest["state"]) if latest else None
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM episode_state "
                f"WHERE subscription_id = ?{scope} GROUP BY state",
                params,
            ).fetchall()
        counts = {str(row["state"]): int(row["n"]) for row in rows}
        organized = counts.get("organized", 0)
        in_flight = sum(
            count for state, count in counts.items() if state in {"grabbed", "downloading", "completed"}
        )
        downloaded = organized + in_flight

        available = sub.available_episode if sub.kind == "tv" else None
        waiting: int | None = None
        unaired: int | None = None
        if sub.kind == "tv":
            if available is not None:
                aired = max(0, available - (lower or 1) + 1)
                if total is not None:
                    aired = min(total, aired)
                waiting = max(0, aired - downloaded)
                if total is not None:
                    unaired = max(0, total - max(aired, downloaded))
            elif total is not None:
                waiting = max(0, total - downloaded)

        return Progress(
            subscription_id=sub_id,
            kind=sub.kind,
            organized=organized,
            in_flight=in_flight,
            total_target=total,
            available=available,
            waiting=waiting,
            unaired=unaired,
            episodes_source=sub.episodes_source,
            movie_state=movie_state,
        )

    def episode_states(self, sub_id: int) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT season, episode, torrent_id, state FROM episode_state "
                "WHERE subscription_id = ? ORDER BY season, episode",
                (sub_id,),
            ).fetchall()
        return [dict(row) for row in rows]
