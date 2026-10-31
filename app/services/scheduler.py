"""APScheduler wiring and job bookkeeping (design §8.9)."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import Settings
from ..db import Database
from ..errors import BudgetExceeded, CooldownActive
from ..timeutil import hours_ago_iso, to_iso, utc_now
from . import settings_store
from .audit import log_event
from .organizer import Organizer
from .poller import Poller
from .search_service import SearchService

log = logging.getLogger("mtbox.scheduler")

EXPECTED_ERRORS = (BudgetExceeded, CooldownActive)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        search: SearchService,
        poller: Poller,
        organizer: Organizer,
        *,
        guard: Any = None,
        seeding: Any = None,
        notifier: Any = None,
        check_lock: threading.Lock | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.search = search
        self.poller = poller
        self.organizer = organizer
        self.guard = guard
        self.seeding = seeding
        self.notifier = notifier
        self.check_lock = check_lock or threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._scheduler: BackgroundScheduler | None = None

    # ---------- lifecycle ----------

    def start(self) -> BackgroundScheduler | None:
        if not self.settings.scheduler_enabled:
            log.info("调度器未启用（SCHEDULER_ENABLED=false）")
            return None
        scheduler = BackgroundScheduler(timezone=self.settings.tz, daemon=True)
        scheduler.add_job(
            lambda: self._job("tick_subscriptions", self.tick_subscriptions, lock=self.check_lock),
            IntervalTrigger(seconds=60),
            id="tick_subscriptions",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("poll_torrents", self.poll_torrents),
            IntervalTrigger(seconds=60),
            id="poll_torrents",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("organize_pending", self.organize_pending),
            IntervalTrigger(seconds=30),
            id="organize_pending",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("profile_guard", self.profile_guard),
            IntervalTrigger(minutes=15),
            id="profile_guard",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("seed_cleanup", self.seed_cleanup),
            IntervalTrigger(hours=1),
            id="seed_cleanup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("prune", self.prune),
            CronTrigger(hour=4, minute=10),
            id="prune",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: self._job("daily_digest", self.daily_digest),
            CronTrigger(hour=9, minute=0),
            id="daily_digest",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info("调度器已启动：%s 个任务", len(scheduler.get_jobs()))
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("调度器已停止")

    # ---------- jobs ----------

    def tick_subscriptions(self) -> None:
        summaries = self.search.check_due()
        if summaries:
            errors = sum(1 for item in summaries if item.error)
            grabbed = sum(item.grabbed for item in summaries)
            log.info(
                "订阅检查：%s 个，错误 %s，抓取 %s", len(summaries), errors, grabbed
            )

    def poll_torrents(self) -> None:
        summary = self.poller.poll()
        if summary.error:
            raise RuntimeError(summary.error)
        if summary.polled:
            log.info(
                "轮询：%s 个，完成 %s，失败 %s", summary.polled, summary.completed, summary.failed
            )

    def organize_pending(self) -> None:
        results = self.organizer.organize_pending()
        if results:
            ok = sum(1 for result in results if result.ok)
            log.info("整理：%s 个，成功 %s", len(results), ok)

    def profile_guard(self) -> None:
        if self.guard is None:
            return
        state = self.guard.refresh()
        log.info("分享率护栏：%s（%s）", state.mode, state.ratio)

    def seed_cleanup(self) -> None:
        if self.seeding is None:
            return
        summary = self.seeding.cleanup()
        if summary.error:
            raise RuntimeError(summary.error)
        if summary.deleted:
            log.info(
                "保种清理：删除 %s 个%s", summary.deleted, "（DRY_RUN）" if summary.dry_run else ""
            )

    def daily_digest(self) -> None:
        with self.db.read() as conn:
            counts = {
                "candidates": conn.execute(
                    "SELECT COUNT(*) AS n FROM torrent t "
                    "JOIN subscription s ON s.id = t.subscription_id "
                    "WHERE t.state = 'candidate' AND s.deleted_at IS NULL"
                ).fetchone()["n"],
                "downloading": conn.execute(
                    "SELECT COUNT(*) AS n FROM torrent t "
                    "JOIN subscription s ON s.id = t.subscription_id "
                    "WHERE t.state IN ('grabbed','downloading') AND s.deleted_at IS NULL"
                ).fetchone()["n"],
                "to_organize": conn.execute(
                    "SELECT COUNT(*) AS n FROM torrent t "
                    "JOIN subscription s ON s.id = t.subscription_id "
                    "WHERE t.state IN ('completed','organize_failed') "
                    "AND s.deleted_at IS NULL"
                ).fetchone()["n"],
                "errors": conn.execute(
                    "SELECT COUNT(*) AS n FROM event_log WHERE level = 'ERROR' AND created_at >= ?",
                    (hours_ago_iso(24),),
                ).fetchone()["n"],
            }
            snapshot = conn.execute(
                "SELECT ratio FROM account_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()
        ratio = snapshot["ratio"] if snapshot and snapshot["ratio"] is not None else None
        usage = self.seeding.disk_usage() if self.seeding is not None else None
        lines = [
            f"待确认候选：{counts['candidates']}",
            f"下载中：{counts['downloading']}",
            f"待整理：{counts['to_organize']}",
            f"24h 错误事件：{counts['errors']}",
            f"分享率：{ratio:.2f}" if ratio is not None else "分享率：未知",
        ]
        if usage is not None:
            lines.append(f"媒体库磁盘：已用 {usage.percent:.1f}%（剩余 {usage.free_gb:.1f} GB）")
        message = "\n".join(lines)
        if self.notifier is not None:
            self.notifier.send("mtbox 运行日报", message)
        log.info("运行日报：%s", "；".join(lines))

    def prune(self) -> None:
        ttl_days = max(1, settings_store.get_int(self.db, "candidate_ttl_days", 7))
        cutoff = hours_ago_iso(ttl_days * 24)
        with self.db.tx() as conn:
            removed_candidates = conn.execute(
                "DELETE FROM torrent WHERE state = 'candidate' AND created_at < ?", (cutoff,)
            ).rowcount
            removed_ignored = conn.execute(
                "DELETE FROM torrent WHERE state = 'ignored' AND ignored_by = 'auto' "
                "AND created_at < ?",
                (cutoff,),
            ).rowcount
            conn.execute("DELETE FROM api_call WHERE called_at < ?", (hours_ago_iso(25),))
            conn.execute(
                "DELETE FROM event_log WHERE created_at < ?", (hours_ago_iso(24 * 30),)
            )
            conn.execute(
                "DELETE FROM account_snapshot WHERE captured_at < ?", (hours_ago_iso(24 * 30),)
            )
        if removed_candidates or removed_ignored:
            log_event(
                self.db,
                "INFO",
                "scheduler",
                "清理过期候选",
                {"candidates": removed_candidates, "ignored": removed_ignored},
            )
        log.info(
            "清理完成：候选 %s，自动忽略 %s", removed_candidates, removed_ignored
        )

    # ---------- wrapper ----------

    def _job(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        lock: threading.Lock | None = None,
    ) -> None:
        gate = lock or self._locks[name]
        if not gate.acquire(blocking=False):
            log.info("任务 %s 正在运行，跳过本次", name)
            return
        started = to_iso(utc_now())
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO job_run(name, last_started_at, ok, error) VALUES (?, ?, NULL, NULL) "
                    "ON CONFLICT(name) DO UPDATE SET last_started_at = excluded.last_started_at, "
                    "ok = NULL, error = NULL",
                    (name, started),
                )
            fn()
        except EXPECTED_ERRORS as exc:
            log.info("任务 %s 被限流，稍后重试: %s", name, exc)
            self._finish(name, ok=False, error=str(exc))
        except Exception as exc:
            log.exception("任务 %s 失败", name)
            self._finish(name, ok=False, error=str(exc))
            log_event(
                self.db,
                "ERROR",
                "scheduler",
                f"定时任务失败: {name}",
                {"error": str(exc)},
            )
        else:
            self._finish(name, ok=True)
        finally:
            gate.release()

    def _finish(self, name: str, *, ok: bool, error: str | None = None) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE job_run SET last_finished_at = ?, ok = ?, error = ? WHERE name = ?",
                (to_iso(utc_now()), 1 if ok else 0, error[:2000] if error else None, name),
            )

    # ---------- introspection ----------

    def status(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM job_run ORDER BY name").fetchall()
        runs = {str(row["name"]): dict(row) for row in rows}
        scheduled = {}
        if self._scheduler is not None:
            for job in self._scheduler.get_jobs():
                scheduled[job.id] = job
        for name in (
            "tick_subscriptions",
            "poll_torrents",
            "organize_pending",
            "profile_guard",
            "seed_cleanup",
            "prune",
            "daily_digest",
        ):
            info = runs.get(
                name,
                {
                    "name": name,
                    "last_started_at": None,
                    "last_finished_at": None,
                    "ok": None,
                    "error": None,
                },
            )
            job = scheduled.get(name)
            info["next_run_at"] = (
                job.next_run_time.isoformat() if job is not None and job.next_run_time else None
            )
            jobs.append(info)
        return jobs
