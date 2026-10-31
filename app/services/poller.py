"""Poll qBittorrent and advance torrent / episode states."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..clients.qbittorrent import QBittorrentClient, QBTorrent
from ..db import Database
from ..errors import QBError
from ..timeutil import parse_dt, to_iso, utc_now
from .audit import log_event

log = logging.getLogger("mtbox.poller")

MISSING_GRACE_SECONDS = 600
ERROR_STATES = {"error", "missingFiles"}


@dataclass(frozen=True, slots=True)
class PollSummary:
    polled: int
    completed: int
    failed: int
    progressed: int
    error: str | None = None


class Poller:
    def __init__(self, db: Database, qb: QBittorrentClient) -> None:
        self.db = db
        self.qb = qb

    def poll(self) -> PollSummary:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM torrent WHERE state IN ('grabbed','downloading') "
                "ORDER BY id"
            ).fetchall()

        if not rows:
            return PollSummary(0, 0, 0, 0)

        by_hash: dict[str, QBTorrent] = {}
        hashes = [str(row["qb_hash"]) for row in rows if row["qb_hash"]]
        if hashes:
            try:
                by_hash = {item.hash: item for item in self.qb.info(hashes)}
            except QBError as exc:
                log.warning("轮询 qBittorrent 失败: %s", exc)
                return PollSummary(0, 0, 0, 0, error=str(exc))

        completed = failed = progressed = 0
        now = utc_now()
        for row in rows:
            qb_hash = row["qb_hash"]
            if not qb_hash:
                if self._older_than(row["grabbed_at"], now, MISSING_GRACE_SECONDS):
                    self._fail(row["id"], row["subscription_id"], "qBittorrent 中没有该任务")
                    failed += 1
                continue

            item = by_hash.get(str(qb_hash).lower())
            if item is None:
                if self._older_than(row["grabbed_at"], now, MISSING_GRACE_SECONDS):
                    self._fail(row["id"], row["subscription_id"], "qBittorrent 中找不到任务")
                    failed += 1
                continue

            if item.state in ERROR_STATES:
                self._fail(row["id"], row["subscription_id"], f"qB 状态异常: {item.state}")
                failed += 1
                continue

            if item.progress >= 0.999:
                if row["state"] != "completed":
                    self._complete(row, item)
                    completed += 1
                continue

            if row["state"] == "grabbed":
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE torrent SET state = 'downloading', progress = ?, updated_at = ? "
                        "WHERE id = ? AND state = 'grabbed'",
                        (item.progress, to_iso(now), row["id"]),
                    )
                progressed += 1
            else:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE torrent SET progress = ?, updated_at = ? WHERE id = ?",
                        (item.progress, to_iso(now), row["id"]),
                    )

        if completed or failed:
            log_event(
                self.db,
                "INFO",
                "poller",
                f"轮询完成：完成 {completed}，失败 {failed}",
                {"polled": len(rows), "completed": completed, "failed": failed},
            )
        return PollSummary(len(rows), completed, failed, progressed)

    def _complete(self, row: object, item: QBTorrent) -> None:
        now_iso = to_iso(utc_now())
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET state = 'completed', progress = 1.0, completed_at = ?, "
                "seed_started_at = COALESCE(seed_started_at, ?), updated_at = ?, "
                "content_path = COALESCE(NULLIF(?, ''), content_path) "
                "WHERE id = ? AND state IN ('grabbed','downloading')",
                (now_iso, now_iso, now_iso, item.content_path or "", row["id"]),
            )
            conn.execute(
                "UPDATE episode_state SET state = 'completed', updated_at = ? "
                "WHERE torrent_id = ? AND state != 'organized'",
                (now_iso, row["id"]),
            )
        log_event(
            self.db,
            "INFO",
            "poller",
            "下载完成",
            {"torrent_id": row["id"], "name": item.name},
        )

    def _fail(self, torrent_id: int, subscription_id: int, error: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error[:2000], to_iso(utc_now()), torrent_id),
            )
        log_event(
            self.db,
            "ERROR",
            "poller",
            "下载任务失败",
            {"torrent_id": torrent_id, "subscription_id": subscription_id, "error": error},
        )

    @staticmethod
    def _older_than(value: object, now: datetime, seconds: int) -> bool:
        parsed = parse_dt(value)
        if parsed is None:
            return True
        return now - parsed >= timedelta(seconds=seconds)
