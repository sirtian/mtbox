"""Event log: the UI's activity feed and post-mortem trail."""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..timeutil import hours_ago_iso, utc_now_iso

MAX_MESSAGE_CHARS = 2000
MAX_PAYLOAD_CHARS = 8000


def log_event(
    db: Database,
    level: str,
    source: str,
    message: str,
    payload: Any = None,
) -> None:
    text = message[:MAX_MESSAGE_CHARS]
    encoded: str | None = None
    if payload is not None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            encoded = json.dumps({"repr": repr(payload)}, ensure_ascii=False)
        if len(encoded) > MAX_PAYLOAD_CHARS:
            encoded = encoded[:MAX_PAYLOAD_CHARS]
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO event_log(level, source, message, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (level.upper(), source, text, encoded, utc_now_iso()),
        )


def recent_event(
    db: Database, *, source: str, message: str, hours: float = 6.0
) -> bool:
    """True when the same event was already logged recently (alert throttling)."""
    with db.read() as conn:
        row = conn.execute(
            "SELECT 1 FROM event_log WHERE source = ? AND message = ? AND created_at >= ? LIMIT 1",
            (source, message, hours_ago_iso(hours)),
        ).fetchone()
    return row is not None


def recent_events(
    db: Database,
    *,
    limit: int = 100,
    level: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if level:
        clauses.append("level = ?")
        params.append(level.upper())
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 1000)))
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT id, level, source, message, payload, created_at "
            f"FROM event_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]
