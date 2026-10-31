from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.qbittorrent import QBTorrent
from app.errors import QBError
from app.services.seeding import SeedingService
from app.services.storage import DiskUsage
from app.timeutil import utc_now_iso
from tests.helpers import insert_subscription, insert_torrent, make_db, make_settings


def qb_item(
    info_hash: str,
    *,
    ratio: float = 1.5,
    seeding_hours: float = 100,
) -> QBTorrent:
    return QBTorrent(
        hash=info_hash,
        name="示例",
        state="uploading",
        progress=1.0,
        size=1_000_000_000,
        downloaded=1_000_000_000,
        save_path="/downloads",
        content_path="/downloads/示例",
        ratio=ratio,
        seeding_time=int(seeding_hours * 3600),
        added_on=0,
        completion_on=0,
        category="mtbox",
        tags="mtbox",
    )


class FakeQB:
    def __init__(self, items: list[QBTorrent], *, error: str | None = None) -> None:
        self.items = {item.hash: item for item in items}
        self.error = error
        self.deleted: list[tuple[list[str], bool]] = []

    def info(self, hashes: list[str]) -> list[QBTorrent]:
        if self.error:
            raise QBError(self.error)
        return [self.items[item] for item in hashes if item in self.items]

    def delete(self, hashes: list[str], *, delete_files: bool) -> None:
        self.deleted.append((list(hashes), delete_files))
        for item in hashes:
            self.items.pop(item, None)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, *, level: str = "info"):
        self.messages.append((title, message, level))
        return ("fake",)


def usage(percent: float) -> DiskUsage:
    return DiskUsage(total_gb=100.0, used_gb=percent, free_gb=100 - percent, percent=percent)


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    return settings, db


def organized(
    env,
    *,
    status: str = "active",
    kind: str = "movie",
    sub_episode_from: int | None = None,
    sub_episode_to: int | None = None,
    **kwargs,
):
    settings, db = env
    sub_id = insert_subscription(
        db,
        kind=kind,
        title="示例",
        status=status,
        episode_from=sub_episode_from,
        episode_to=sub_episode_to,
    )
    torrent_id = insert_torrent(
        db,
        sub_id,
        mt_id=kwargs.pop("mt_id", "1"),
        state="organized",
        qb_hash=kwargs.pop("qb_hash", "hash1"),
        **kwargs,
    )
    return sub_id, torrent_id


def get_torrent(db, torrent_id: int):
    with db.read() as conn:
        return conn.execute("SELECT * FROM torrent WHERE id = ?", (torrent_id,)).fetchone()


def test_deletes_when_ratio_and_time_met(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=1.3, seeding_hours=100)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.error is None
    assert summary.deleted == 1
    assert qb.deleted == [(["hash1"], True)]
    assert get_torrent(db, torrent_id)["state"] == "deleted"
    assert get_torrent(db, torrent_id)["deleted_at"]


def test_keeps_below_min_seed_hours(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=5.0, seeding_hours=10)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 0
    assert qb.deleted == []
    assert get_torrent(db, torrent_id)["state"] == "organized"


def test_max_hours_forces_cleanup(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=0.2, seeding_hours=400)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 1
    assert get_torrent(db, torrent_id)["state"] == "deleted"


def test_low_ratio_below_max_hours_kept(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=0.2, seeding_hours=100)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 0
    assert get_torrent(db, torrent_id)["state"] == "organized"


def test_keeps_open_ended_active_tv(env) -> None:
    settings, db = env
    _, torrent_id = organized(env, kind="tv")
    qb = FakeQB([qb_item("hash1", ratio=3.0, seeding_hours=500)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 0
    assert get_torrent(db, torrent_id)["state"] == "organized"


def test_keeps_incomplete_bounded_tv(env) -> None:
    settings, db = env
    sub_id, torrent_id = organized(
        env, kind="tv", sub_episode_from=1, sub_episode_to=3, season=1
    )
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, ?, 'organized', ?)",
            (sub_id, torrent_id, utc_now_iso()),
        )
    qb = FakeQB([qb_item("hash1", ratio=3.0, seeding_hours=500)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 0


def test_deletes_completed_bounded_tv(env) -> None:
    settings, db = env
    sub_id, torrent_id = organized(
        env, kind="tv", sub_episode_from=1, sub_episode_to=2, status="done"
    )
    qb = FakeQB([qb_item("hash1", ratio=3.0, seeding_hours=500)])
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.deleted == 1


def test_dry_run_does_not_delete(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=1.3, seeding_hours=100)])
    summary = SeedingService(settings, db, qb).cleanup(dry_run=True)
    assert summary.dry_run is True
    assert summary.deleted == 1
    assert qb.deleted == []
    assert get_torrent(db, torrent_id)["state"] == "organized"
    assert summary.items[0].reason


def test_disk_pressure_deletes_until_below_threshold(env, monkeypatch) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([qb_item("hash1", ratio=0.1, seeding_hours=100)])
    notifier = FakeNotifier()
    service = SeedingService(settings, db, qb, notifier=notifier)
    values = iter([usage(95.0), usage(95.0), usage(80.0)])
    calls: list[float] = []

    def fake_usage():
        value = next(values, usage(80.0))
        calls.append(value.percent)
        return value

    monkeypatch.setattr(service, "disk_usage", fake_usage)
    summary = service.cleanup()
    assert summary.pressure is True
    assert summary.deleted == 1
    assert get_torrent(db, torrent_id)["state"] == "deleted"
    assert len(notifier.messages) == 1
    assert notifier.messages[0][2] == "warn"
    assert calls[-1] == 80.0


def test_qb_error_returns_error(env) -> None:
    settings, db = env
    _, torrent_id = organized(env)
    qb = FakeQB([], error="qB 未启动")
    summary = SeedingService(settings, db, qb).cleanup()
    assert summary.error == "qB 未启动"
    assert summary.deleted == 0
    assert get_torrent(db, torrent_id)["state"] == "organized"


def test_organized_episode_state_is_kept(env) -> None:
    settings, db = env
    sub_id, torrent_id = organized(env, kind="tv", status="done")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, ?, 'organized', ?)",
            (sub_id, torrent_id, utc_now_iso()),
        )
    qb = FakeQB([qb_item("hash1", ratio=3.0, seeding_hours=500)])
    SeedingService(settings, db, qb).cleanup()
    with db.read() as conn:
        row = conn.execute(
            "SELECT state FROM episode_state WHERE torrent_id = ?", (torrent_id,)
        ).fetchone()
    assert row["state"] == "organized"
