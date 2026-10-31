"""Seeding cleanup and disk pressure checks (design §4.3 / §8.6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..clients.qbittorrent import QBittorrentClient, QBTorrent
from ..config import Settings
from ..db import Database
from ..errors import QBError
from ..timeutil import hours_ago_iso, to_iso, utc_now
from . import settings_store
from .audit import log_event
from .storage import DiskUsage, disk_usage

log = logging.getLogger("mtbox.seeding")

DISK_ALERT_INTERVAL_HOURS = 6


@dataclass(frozen=True, slots=True)
class Deletion:
    torrent_id: int
    title: str
    hash: str
    seeding_hours: float
    ratio: float
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    checked: int = 0
    deleted: int = 0
    skipped: int = 0
    dry_run: bool = False
    pressure: bool = False
    error: str | None = None
    items: tuple[Deletion, ...] = field(default_factory=tuple)


class SeedingService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        qb: QBittorrentClient,
        notifier: Any = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.qb = qb
        self.notifier = notifier

    def disk_usage(self) -> DiskUsage | None:
        return disk_usage(self.settings.library_dir)

    def cleanup(self, dry_run: bool | None = None) -> CleanupSummary:
        dry_run = self.settings.dry_run if dry_run is None else dry_run
        usage = self.disk_usage()
        threshold = settings_store.get_int(self.db, "disk_pause_percent", 90)
        pressure = bool(usage and threshold and usage.percent >= threshold)

        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT t.id, t.title, t.size_bytes, t.qb_hash, t.seed_started_at, "
                "s.id AS sub_id, s.kind AS sub_kind, s.status AS sub_status, "
                "s.season AS sub_season, s.episode_from, s.episode_to "
                "FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
                "WHERE t.state = 'organized' AND t.qb_hash IS NOT NULL "
                "ORDER BY t.id"
            ).fetchall()
        if not rows:
            return CleanupSummary(dry_run=dry_run, pressure=pressure)

        hashes = [str(row["qb_hash"]) for row in rows]
        try:
            live = {item.hash: item for item in self.qb.info(hashes)}
        except QBError as exc:
            log.warning("读取做种状态失败: %s", exc)
            return CleanupSummary(
                checked=len(rows), dry_run=dry_run, pressure=pressure, error=str(exc)
            )

        min_hours = max(0, settings_store.get_int(self.db, "min_seed_hours", 72))
        max_hours = max(0, settings_store.get_int(self.db, "max_seed_hours", 336))
        min_ratio = max(0.0, settings_store.get_float(self.db, "min_ratio", 1.0))

        eligible: list[tuple[Any, QBTorrent, float, str]] = []
        for row in rows:
            item = live.get(str(row["qb_hash"]).lower())
            if item is None:
                continue
            hours = max(0.0, item.seeding_time / 3600.0)
            if hours < min_hours:
                continue
            met_ratio = item.ratio >= min_ratio
            met_time = bool(max_hours and hours >= max_hours)
            if not met_ratio and not met_time and not pressure:
                continue
            if self._still_chasing(row):
                continue
            reasons: list[str] = []
            if met_ratio:
                reasons.append(f"分享率 {item.ratio:.2f} ≥ {min_ratio:.2f}")
            if met_time:
                reasons.append(f"做种 {hours:.0f}h ≥ {max_hours}h")
            if not reasons:
                reasons.append(f"磁盘压力（已做种 {hours:.0f}h）")
            eligible.append((row, item, hours, "；".join(reasons)))

        eligible.sort(key=lambda entry: entry[2], reverse=True)

        deleted: list[Deletion] = []
        skipped = len(rows) - len(eligible)
        error: str | None = None
        now = to_iso(utc_now())
        for row, item, hours, reason in eligible:
            if pressure and not dry_run:
                current = self.disk_usage()
                if current is not None and threshold and current.percent < threshold:
                    break
            deletion = Deletion(
                torrent_id=int(row["id"]),
                title=str(row["title"]),
                hash=item.hash,
                seeding_hours=round(hours, 1),
                ratio=round(item.ratio, 3),
                reason=reason,
            )
            if dry_run:
                deleted.append(deletion)
                continue
            try:
                self.qb.delete([item.hash], delete_files=True)
            except QBError as exc:
                error = str(exc)
                log.warning("删除做种任务失败 %s: %s", row["id"], exc)
                break
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE torrent SET state = 'deleted', deleted_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (now, now, row["id"]),
                )
            deleted.append(deletion)
            # episode_state intentionally kept for organized episodes.

        if deleted:
            log_event(
                self.db,
                "INFO",
                "seeding",
                f"清理做种 {len(deleted)} 个" + ("（试运行）" if dry_run else ""),
                {"deleted": [item.torrent_id for item in deleted], "dry_run": dry_run},
            )
            log.info("保种清理：%s 个%s", len(deleted), "（DRY_RUN）" if dry_run else "")

        if pressure:
            self._notify_pressure(usage, threshold, len(deleted), dry_run)
            if not dry_run:
                after = self.disk_usage()
                if after is not None and threshold and after.percent >= threshold:
                    log_event(
                        self.db,
                        "WARN",
                        "seeding",
                        "清理后磁盘水位仍高于阈值",
                        {"percent": after.percent, "threshold": threshold},
                    )

        return CleanupSummary(
            checked=len(rows),
            deleted=len(deleted),
            skipped=skipped,
            dry_run=dry_run,
            pressure=pressure,
            error=error,
            items=tuple(deleted),
        )

    # ---------- helpers ----------

    def _still_chasing(self, row: Any) -> bool:
        if str(row["sub_kind"]) != "tv" or str(row["sub_status"]) != "active":
            return False
        if row["episode_to"] is None:
            return True
        start = int(row["episode_from"] or 1)
        total = int(row["episode_to"]) - start + 1
        if total <= 0:
            return False
        with self.db.read() as conn:
            organized = conn.execute(
                "SELECT COUNT(*) AS n FROM episode_state "
                "WHERE subscription_id = ? AND season = ? AND state = 'organized' "
                "AND episode BETWEEN ? AND ?",
                (row["sub_id"], row["sub_season"], start, int(row["episode_to"])),
            ).fetchone()
        return int(organized["n"]) < total

    def _notify_pressure(
        self, usage: DiskUsage | None, threshold: int, deleted: int, dry_run: bool
    ) -> None:
        if usage is None:
            return
        with self.db.read() as conn:
            recent = conn.execute(
                "SELECT 1 FROM event_log WHERE source = 'seeding' AND level = 'WARN' "
                "AND message LIKE '磁盘水位%' AND created_at >= ? LIMIT 1",
                (hours_ago_iso(DISK_ALERT_INTERVAL_HOURS),),
            ).fetchone()
        if recent:
            return
        message = (
            f"媒体库磁盘已用 {usage.percent:.1f}%（阈值 {threshold}%），"
            f"本次清理 {deleted} 个做种{'（试运行）' if dry_run else ''}"
        )
        log_event(self.db, "WARN", "seeding", "磁盘水位偏高", {"percent": usage.percent})
        if self.notifier is not None:
            self.notifier.send("mtbox 磁盘告警", message, level="warn")
