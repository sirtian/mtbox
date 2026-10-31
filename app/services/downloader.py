"""Candidate → qBittorrent grab pipeline."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..clients.bencode import BencodeError, TorrentMeta, parse_torrent
from ..clients.mteam import MTeamClient
from ..clients.qbittorrent import QBittorrentClient
from ..config import Settings
from ..db import Database
from ..errors import Conflict, MTBoxError, MTeamNetworkError, NotFound
from ..models import Subscription, Torrent
from ..timeutil import to_iso, utc_now
from . import profile_guard, settings_store
from .audit import log_event
from .matcher import is_free
from .storage import disk_usage
from .subscriptions import SubscriptionService

log = logging.getLogger("mtbox.downloader")

OUTCOME_GRABBED = "grabbed"
OUTCOME_DRY_RUN = "dry_run"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GrabResult:
    torrent_id: int
    outcome: str
    message: str


class DownloadService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        mt: MTeamClient,
        qb: QBittorrentClient,
        notifier: Any = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.mt = mt
        self.qb = qb
        self.notifier = notifier
        self.subscriptions = SubscriptionService(db)

    def get_torrent(self, torrent_id: int) -> Torrent:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
        if not row:
            raise NotFound(f"任务 {torrent_id} 不存在")
        return Torrent.from_row(row)

    def grab(self, torrent_id: int, *, actor: str = "manual") -> GrabResult:
        torrent = self.get_torrent(torrent_id)
        if torrent.state != "candidate":
            return GrabResult(torrent.id, OUTCOME_SKIPPED, f"当前状态为 {torrent.state}")

        sub = self.subscriptions.get(torrent.subscription_id)
        self._check_guards(torrent, sub)

        if self.settings.dry_run:
            log_event(
                self.db,
                "INFO",
                "download",
                f"DRY_RUN: would grab mt_id={torrent.mt_id}",
                {"torrent_id": torrent.id, "actor": actor},
            )
            return GrabResult(torrent.id, OUTCOME_DRY_RUN, "DRY_RUN 已开启，未抓取")

        covered = torrent.covered_episodes()
        missing = self._missing_episodes(sub, torrent, covered)
        if covered and not missing:
            raise Conflict("该种子覆盖的集数已全部处理")

        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE torrent SET state = 'grabbed', grabbed_at = ?, updated_at = ?, error = NULL "
                "WHERE id = ? AND state = 'candidate'",
                (to_iso(utc_now()), to_iso(utc_now()), torrent.id),
            )
            if cursor.rowcount == 0:
                return GrabResult(torrent.id, OUTCOME_SKIPPED, "任务已被其他操作处理")
        log_event(
            self.db,
            "INFO",
            "download",
            f"开始抓取 {torrent.title}",
            {"torrent_id": torrent.id, "mt_id": torrent.mt_id, "actor": actor},
        )

        try:
            data = self._fetch_torrent(torrent.mt_id)
            meta = parse_torrent(data)
            save_path = self.settings.qb_save_path
            files = self._file_entries(meta)
            self._store_torrent_file(torrent.mt_id, data)

            # Persist identity before touching qB so a timeout can be recovered by hash.
            now = to_iso(utc_now())
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE torrent SET qb_hash = ?, files = ?, save_path = ?, updated_at = ? "
                    "WHERE id = ?",
                    (meta.info_hash, _json(files), save_path, now, torrent.id),
                )

            seeding_hours = settings_store.get_int(self.db, "min_seed_hours", 72)
            min_ratio = settings_store.get_float(self.db, "min_ratio", 1.0)
            self.qb.ensure_category(self.settings.qb_category)
            existing = {item.hash for item in self.qb.info([meta.info_hash])}
            if meta.info_hash not in existing:
                self.qb.add(
                    data,
                    save_path=save_path,
                    category=self.settings.qb_category,
                    tags=(self.settings.qb_tags,) if self.settings.qb_tags else (),
                    paused=False,
                    seeding_time_limit_min=seeding_hours * 60,
                    ratio_limit=min_ratio,
                )

            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE torrent SET state = 'downloading', updated_at = ?, error = NULL "
                    "WHERE id = ? AND state = 'grabbed'",
                    (to_iso(utc_now()), torrent.id),
                )
            self._write_episode_states(sub, torrent, missing, "grabbed")
            log_event(
                self.db,
                "INFO",
                "download",
                f"已添加到 qBittorrent: {torrent.title}",
                {"torrent_id": torrent.id, "info_hash": meta.info_hash},
            )
            self._notify(
                "mtbox 抓取成功",
                f"{torrent.title}\n{torrent.size_bytes / (1024**3):.2f} GB",
            )
            return GrabResult(torrent.id, OUTCOME_GRABBED, "已加入下载")
        except (MTBoxError, BencodeError, OSError) as exc:
            self._mark_failed(torrent.id, str(exc))
            return GrabResult(torrent.id, OUTCOME_FAILED, str(exc))
        except Exception as exc:  # defensive: never leave the row stuck in grabbed
            log.exception("抓取失败")
            self._mark_failed(torrent.id, f"未预期错误: {exc}")
            return GrabResult(torrent.id, OUTCOME_FAILED, str(exc))

    # ---------- internals ----------

    def _check_guards(self, torrent: Torrent, _sub: Subscription) -> None:
        guard = profile_guard.load(self.db)
        free = is_free(torrent.discount)
        if guard.paused:
            raise Conflict("分享率过低，已暂停抓取")
        if guard.free_only and not free:
            raise Conflict("分享率偏低，仅允许免费种子")
        if free:
            return
        usage = disk_usage(self.settings.library_dir)
        threshold = settings_store.get_int(self.db, "disk_pause_percent", 90)
        if usage is not None and threshold and usage.percent >= threshold:
            raise Conflict(
                f"磁盘水位 {usage.percent:.1f}% ≥ {threshold}%，暂停非免费抓取"
            )

    def _notify(self, title: str, message: str, *, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(title, message, level=level)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.warning("发送通知失败", exc_info=True)

    def _fetch_torrent(self, mt_id: str) -> bytes:
        last_error: MTeamNetworkError | None = None
        for attempt in range(3):
            try:
                url = self.mt.gen_download_url(mt_id)
                return self.mt.download_torrent(url)
            except MTeamNetworkError as exc:
                last_error = exc
                log.warning("取种网络失败（第 %s 次）: %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
        raise last_error or MTeamNetworkError("取种失败")

    def _store_torrent_file(self, mt_id: str, data: bytes) -> None:
        safe = "".join(ch for ch in str(mt_id) if ch.isalnum() or ch in "-_")
        target = self.settings.torrents_dir / f"{safe}.torrent"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    @staticmethod
    def _file_entries(meta: TorrentMeta) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        # qBittorrent creates a folder for any multi-file torrent, even when the
        # file list has a single entry, so follow the torrent layout, not the count.
        for item in meta.files:
            path = f"{meta.name}/{item.path}" if meta.is_multi else item.path
            entries.append({"path": path, "size": item.length})
        return entries

    def _missing_episodes(
        self, sub: Subscription, torrent: Torrent, covered: list[int]
    ) -> list[int]:
        if not covered:
            return []
        season = torrent.season if torrent.season is not None else sub.season
        placeholders = ", ".join("?" for _ in covered)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT episode FROM episode_state WHERE subscription_id = ? AND season = ? "
                f"AND episode IN ({placeholders}) AND state IN "
                "('grabbed','downloading','completed','organized')",
                (sub.id, season, *covered),
            ).fetchall()
        existing = {int(row["episode"]) for row in rows}
        return [episode for episode in covered if episode not in existing]

    def _write_episode_states(
        self, sub: Subscription, torrent: Torrent, episodes: list[int], state: str
    ) -> None:
        if not episodes:
            return
        season = torrent.season if torrent.season is not None else sub.season
        now = to_iso(utc_now())
        with self.db.tx() as conn:
            for episode in episodes:
                conn.execute(
                    "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(subscription_id, season, episode) DO UPDATE SET "
                    "state = excluded.state, torrent_id = excluded.torrent_id, updated_at = excluded.updated_at "
                    "WHERE episode_state.state != 'organized'",
                    (sub.id, season, episode, torrent.id, state, now),
                )

    def _mark_failed(self, torrent_id: int, error: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error[:2000], to_iso(utc_now()), torrent_id),
            )
        log_event(self.db, "ERROR", "download", "抓取失败", {"torrent_id": torrent_id, "error": error})
        self._notify("mtbox 抓取失败", f"任务 {torrent_id}: {error[:500]}", level="error")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
