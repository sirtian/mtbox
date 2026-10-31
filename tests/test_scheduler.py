from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.poller import PollSummary
from app.services.scheduler import Scheduler
from app.timeutil import days_ago_iso
from tests.helpers import insert_subscription, insert_torrent, make_db, make_settings


class FakeSearch:
    def __init__(self, lock: threading.Lock | None = None) -> None:
        self.calls = 0
        self.lock = lock

    def check_due(self) -> list[object]:
        self.calls += 1
        return []


class FakePoller:
    def __init__(self, error: str | None = None) -> None:
        self.error = error

    def poll(self) -> PollSummary:
        if self.error:
            return PollSummary(0, 0, 0, 0, error=self.error)
        return PollSummary(0, 0, 0, 0)


class FakeOrganizer:
    def organize_pending(self) -> list[object]:
        return []


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    return settings, db


class FakeGuard:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self):
        self.calls += 1
        return SimpleNamespace(mode="ok", ratio=1.5)


class FakeSeeding:
    def __init__(self, *, error: str | None = None, deleted: int = 0) -> None:
        self.error = error
        self.deleted = deleted
        self.calls = 0

    def cleanup(self):
        self.calls += 1
        return SimpleNamespace(
            checked=1,
            deleted=self.deleted,
            skipped=0,
            dry_run=False,
            pressure=False,
            error=self.error,
            items=(),
        )

    def disk_usage(self):
        return SimpleNamespace(percent=42.0, free_gb=100.0, total_gb=200.0, used_gb=100.0)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, *, level: str = "info"):
        self.messages.append((title, message, level))
        return ("fake",)


def make_scheduler(
    settings, db, *, search=None, poller=None, guard=None, seeding=None, notifier=None
) -> Scheduler:
    return Scheduler(  # type: ignore[arg-type]
        settings,
        db,
        search or FakeSearch(),
        poller or FakePoller(),
        FakeOrganizer(),
        guard=guard,
        seeding=seeding,
        notifier=notifier,
    )


def job_row(db, name: str):
    with db.read() as conn:
        return conn.execute("SELECT * FROM job_run WHERE name = ?", (name,)).fetchone()


def test_job_records_success(env) -> None:
    settings, db = env
    scheduler = make_scheduler(settings, db)
    scheduler._job("tick_subscriptions", lambda: None)
    row = job_row(db, "tick_subscriptions")
    assert row["ok"] == 1
    assert row["last_started_at"]
    assert row["last_finished_at"]
    assert row["error"] is None


def test_job_records_failure(env) -> None:
    settings, db = env
    scheduler = make_scheduler(settings, db)

    def boom() -> None:
        raise RuntimeError("模拟失败")

    scheduler._job("poll_torrents", boom)
    row = job_row(db, "poll_torrents")
    assert row["ok"] == 0
    assert "模拟失败" in row["error"]
    with db.read() as conn:
        events = conn.execute(
            "SELECT * FROM event_log WHERE source = 'scheduler' AND level = 'ERROR'"
        ).fetchall()
    assert events


def test_job_skips_when_check_lock_held(env) -> None:
    settings, db = env
    search = FakeSearch()
    scheduler = make_scheduler(settings, db, search=search)
    held = threading.Lock()
    held.acquire()
    scheduler.check_lock = held
    try:
        scheduler._job("tick_subscriptions", scheduler.tick_subscriptions, lock=held)
    finally:
        held.release()
    assert search.calls == 0


def test_tick_calls_search(env) -> None:
    settings, db = env
    search = FakeSearch()
    scheduler = make_scheduler(settings, db, search=search)
    scheduler._job("tick_subscriptions", scheduler.tick_subscriptions)
    assert search.calls == 1


def test_poller_error_is_recorded(env) -> None:
    settings, db = env
    scheduler = make_scheduler(settings, db, poller=FakePoller(error="qB 不可用"))
    scheduler._job("poll_torrents", scheduler.poll_torrents)
    row = job_row(db, "poll_torrents")
    assert row["ok"] == 0
    assert "qB 不可用" in row["error"]


def test_prune_removes_expired_candidates(env) -> None:
    settings, db = env
    sub_id = insert_subscription(db, title="示例剧")
    old_candidate = insert_torrent(db, sub_id, mt_id="1", state="candidate")
    fresh_candidate = insert_torrent(db, sub_id, mt_id="2", state="candidate")
    old_auto = insert_torrent(db, sub_id, mt_id="3", state="ignored")
    old_manual = insert_torrent(db, sub_id, mt_id="4", state="ignored")
    with db.tx() as conn:
        conn.execute(
            "UPDATE torrent SET created_at = ? WHERE id IN (?, ?, ?)",
            (days_ago_iso(10), old_candidate, old_auto, old_manual),
        )
        conn.execute("UPDATE torrent SET ignored_by = 'auto' WHERE id = ?", (old_auto,))
        conn.execute("UPDATE torrent SET ignored_by = 'manual' WHERE id = ?", (old_manual,))

    make_scheduler(settings, db).prune()

    with db.read() as conn:
        remaining = {
            str(row["mt_id"])
            for row in conn.execute("SELECT mt_id FROM torrent").fetchall()
        }
    assert remaining == {"2", "4"}


def test_start_disabled(env) -> None:
    settings, db = env
    settings.scheduler_enabled = False
    assert make_scheduler(settings, db).start() is None


def test_start_and_shutdown(env) -> None:
    settings, db = env
    settings.scheduler_enabled = True
    scheduler = make_scheduler(settings, db)
    background = scheduler.start()
    assert background is not None
    assert {job.id for job in background.get_jobs()} == {
        "tick_subscriptions",
        "poll_torrents",
        "organize_pending",
        "profile_guard",
        "seed_cleanup",
        "prune",
        "daily_digest",
    }
    scheduler.shutdown()
    scheduler.shutdown()  # idempotent


def test_status_lists_jobs(env) -> None:
    settings, db = env
    scheduler = make_scheduler(settings, db)
    scheduler._job("prune", lambda: None)
    status = {item["name"]: item for item in scheduler.status()}
    assert set(status) == {
        "tick_subscriptions",
        "poll_torrents",
        "organize_pending",
        "profile_guard",
        "seed_cleanup",
        "prune",
        "daily_digest",
    }
    assert status["prune"]["ok"] == 1
    assert status["tick_subscriptions"]["last_started_at"] is None


def test_profile_guard_job(env) -> None:
    settings, db = env
    guard = FakeGuard()
    scheduler = make_scheduler(settings, db, guard=guard)
    scheduler._job("profile_guard", scheduler.profile_guard)
    assert guard.calls == 1
    assert job_row(db, "profile_guard")["ok"] == 1


def test_seed_cleanup_error_is_recorded(env) -> None:
    settings, db = env
    seeding = FakeSeeding(error="qB 不可用")
    scheduler = make_scheduler(settings, db, seeding=seeding)
    scheduler._job("seed_cleanup", scheduler.seed_cleanup)
    assert seeding.calls == 1
    row = job_row(db, "seed_cleanup")
    assert row["ok"] == 0
    assert "qB 不可用" in row["error"]


def test_daily_digest_sends_summary(env) -> None:
    settings, db = env
    sub_id = insert_subscription(db, title="示例剧")
    insert_torrent(db, sub_id, mt_id="1", state="candidate")
    notifier = FakeNotifier()
    scheduler = make_scheduler(
        settings, db, seeding=FakeSeeding(), notifier=notifier
    )
    scheduler._job("daily_digest", scheduler.daily_digest)
    assert job_row(db, "daily_digest")["ok"] == 1
    assert len(notifier.messages) == 1
    title, message, _ = notifier.messages[0]
    assert "日报" in title
    assert "待确认候选：1" in message
    assert "媒体库磁盘" in message
