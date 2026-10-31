from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.mteam import MTeamProfile
from app.services import profile_guard
from app.services.profile_guard import GuardState, ProfileGuard, evaluate_mode
from tests.helpers import make_db, make_settings


class FakeMT:
    def __init__(self, ratio: float | None, *, error: Exception | None = None) -> None:
        self.ratio = ratio
        self.error = error
        self.calls = 0

    def profile(self) -> MTeamProfile:
        self.calls += 1
        if self.error:
            raise self.error
        return MTeamProfile(
            ratio=self.ratio,
            uploaded=1000,
            downloaded=100,
            bonus=12.5,
            raw={},
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, *, level: str = "info"):
        self.messages.append((title, message, level))
        return ("fake",)


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    return settings, db


def guard_events(db) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE source = 'guard' ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def test_evaluate_mode_boundaries() -> None:
    assert evaluate_mode(None, 0.6, 0.8) == "ok"
    assert evaluate_mode(1.2, 0.6, 0.8) == "ok"
    assert evaluate_mode(0.8, 0.6, 0.8) == "ok"
    assert evaluate_mode(0.7, 0.6, 0.8) == "free_only"
    assert evaluate_mode(0.6, 0.6, 0.8) == "free_only"
    assert evaluate_mode(0.59, 0.6, 0.8) == "paused"


def test_refresh_writes_snapshot_and_ok(env) -> None:
    settings, db = env
    state = ProfileGuard(settings, db, FakeMT(1.5)).refresh()
    assert state.mode == "ok"
    assert state.ratio == 1.5
    with db.read() as conn:
        row = conn.execute("SELECT * FROM account_snapshot ORDER BY id DESC").fetchone()
    assert row["ratio"] == 1.5
    assert row["uploaded"] == 1000
    assert profile_guard.load(db).mode == "ok"


def test_free_only_and_paused_transitions_notify_once(env) -> None:
    settings, db = env
    notifier = FakeNotifier()
    guard = ProfileGuard(settings, db, FakeMT(0.7), notifier=notifier)
    state = guard.refresh()
    assert state.mode == "free_only"
    assert len(notifier.messages) == 1
    assert notifier.messages[0][2] == "warn"
    events = guard_events(db)
    assert len(events) == 1
    assert events[0]["level"] == "WARN"

    guard.refresh()  # unchanged
    assert len(guard_events(db)) == 1
    assert len(notifier.messages) == 1

    guard.mt = FakeMT(0.4)
    state = guard.refresh()
    assert state.mode == "paused"
    assert len(guard_events(db)) == 2
    assert guard_events(db)[-1]["level"] == "ERROR"
    assert len(notifier.messages) == 2

    guard.mt = FakeMT(1.1)
    state = guard.refresh()
    assert state.mode == "ok"
    assert len(guard_events(db)) == 3
    assert guard_events(db)[-1]["level"] == "INFO"


def test_thresholds_are_configurable(env) -> None:
    settings, db = env
    from app.services import settings_store

    settings_store.set_values(db, {"ratio_pause_all": "0.3", "ratio_pause_nonfree": "0.5"})
    guard = ProfileGuard(settings, db, FakeMT(0.4))
    assert guard.refresh().mode == "free_only"
    guard.mt = FakeMT(0.25)
    assert guard.refresh().mode == "paused"


def test_missing_ratio_keeps_previous_state(env) -> None:
    settings, db = env
    profile_guard.save(db, GuardState(mode="free_only", ratio=0.7, updated_at="2026-01-01T00:00:00Z"))
    state = ProfileGuard(settings, db, FakeMT(None)).refresh()
    assert state.mode == "free_only"
    assert state.ratio == 0.7
    assert guard_events(db) == []


def test_profile_error_keeps_previous_state(env) -> None:
    settings, db = env
    from app.errors import MTeamNetworkError

    profile_guard.save(db, GuardState(mode="paused", ratio=0.4, updated_at="2026-01-01T00:00:00Z"))
    state = ProfileGuard(settings, db, FakeMT(1.0, error=MTeamNetworkError("超时"))).refresh()
    assert state.mode == "paused"
    assert state.ratio == 0.4
    events = guard_events(db)
    assert events and events[-1]["level"] == "WARN"


def insert_snapshot(db, ratio: float | None, captured_at: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO account_snapshot(ratio, uploaded, downloaded, bonus, captured_at) "
            "VALUES (?, 0, 0, 0, ?)",
            (ratio, captured_at),
        )


def test_sync_snapshot_uses_fresh_selfcheck_data(env) -> None:
    settings, db = env
    from app.timeutil import utc_now_iso

    insert_snapshot(db, 0.7, utc_now_iso())
    state = ProfileGuard(settings, db, FakeMT(1.0)).sync_snapshot()
    assert state.mode == "free_only"
    assert state.ratio == 0.7
    assert profile_guard.load(db).mode == "free_only"
    assert guard_events(db)[-1]["level"] == "WARN"


def test_sync_snapshot_ignores_stale_data(env) -> None:
    settings, db = env
    from app.timeutil import hours_ago_iso

    insert_snapshot(db, 0.4, hours_ago_iso(5))
    state = ProfileGuard(settings, db, FakeMT(1.0)).sync_snapshot()
    assert state.mode == "ok"
    assert profile_guard.load(db).mode == "ok"
    assert guard_events(db) == []


def test_auth_error_notifies_and_keeps_state(env) -> None:
    settings, db = env
    from app.errors import MTeamAuthError

    notifier = FakeNotifier()
    profile_guard.save(db, GuardState(mode="ok", ratio=1.2, updated_at="2026-01-01T00:00:00Z"))
    guard = ProfileGuard(
        settings, db, FakeMT(1.0, error=MTeamAuthError("HTTP 401")), notifier=notifier
    )
    state = guard.refresh()
    assert state.mode == "ok"
    assert notifier.messages and notifier.messages[0][2] == "error"
    assert guard_events(db)[-1]["level"] == "ERROR"

    guard.refresh()  # repeated failure: log again, do not spam notifications
    assert len(notifier.messages) == 1
    assert len(guard_events(db)) == 2


def test_load_ignores_corrupt_setting(env) -> None:
    settings, db = env
    from app.services import settings_store

    settings_store.set_values(db, {"profile_guard": "not json"})
    assert profile_guard.load(db).mode == "ok"
    settings_store.set_values(db, {"profile_guard": '{"mode":"bogus"}'})
    assert profile_guard.load(db).mode == "ok"
