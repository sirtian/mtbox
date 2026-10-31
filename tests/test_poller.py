from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.clients.qbittorrent import QBTorrent
from app.services.poller import Poller
from app.timeutil import to_iso, utc_now
from tests.helpers import insert_subscription, insert_torrent, make_db, make_settings


def make_qb(
    hash_value: str,
    *,
    progress: float = 0.5,
    state: str = "downloading",
    content_path: str = "/downloads/Show.S01E01",
) -> QBTorrent:
    return QBTorrent(
        hash=hash_value,
        name="Show.S01E01",
        state=state,
        progress=progress,
        size=1000,
        downloaded=int(1000 * progress),
        save_path="/downloads",
        content_path=content_path,
        ratio=0.0,
        seeding_time=0,
        added_on=0,
        completion_on=0,
        category="mtbox",
        tags="mtbox",
    )


class FakeQB:
    def __init__(self, torrents: list[QBTorrent], *, error: Exception | None = None) -> None:
        self.torrents = torrents
        self.error = error

    def info(self, hashes: list[str]) -> list[QBTorrent]:
        if self.error:
            raise self.error
        return [item for item in self.torrents if item.hash in hashes]


def setup(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "library")
    db = make_db(settings)
    sub_id = insert_subscription(db)
    return settings, db, sub_id


def test_progress_updates_downloading(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    poller = Poller(db, FakeQB([make_qb("abc", progress=0.42)]))  # type: ignore[arg-type]

    summary = poller.poll()

    assert summary.polled == 1
    with db.read() as conn:
        row = conn.execute("SELECT state, progress FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "downloading"
    assert row["progress"] == 0.42


def test_completion_advances_episode_state(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, ?, 'grabbed', '2026-01-01T00:00:00Z')",
            (sub_id, torrent_id),
        )
    poller = Poller(db, FakeQB([make_qb("abc", progress=1.0, state="uploading")]))  # type: ignore[arg-type]

    summary = poller.poll()

    assert summary.completed == 1
    with db.read() as conn:
        row = conn.execute(
            "SELECT state, completed_at, seed_started_at FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
        episode = conn.execute(
            "SELECT state FROM episode_state WHERE subscription_id = ? AND episode = 1", (sub_id,)
        ).fetchone()
    assert row["state"] == "completed"
    assert row["completed_at"] is not None
    assert row["seed_started_at"] is not None
    assert episode["state"] == "completed"


def test_completion_never_downgrades_organized(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, ?, 'organized', '2026-01-01T00:00:00Z')",
            (sub_id, torrent_id),
        )
    Poller(db, FakeQB([make_qb("abc", progress=1.0, state="uploading")])).poll()  # type: ignore[arg-type]

    with db.read() as conn:
        state = conn.execute(
            "SELECT state FROM episode_state WHERE subscription_id = ? AND episode = 1", (sub_id,)
        ).fetchone()[0]
    assert state == "organized"


def test_missing_torrent_after_grace_fails(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    old = to_iso(utc_now() - timedelta(minutes=20))
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=old
    )
    poller = Poller(db, FakeQB([]))  # type: ignore[arg-type]

    summary = poller.poll()

    assert summary.failed == 1
    with db.read() as conn:
        row = conn.execute("SELECT state, error FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "failed"
    assert "找不到" in row["error"]


def test_missing_torrent_within_grace_is_left_alone(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    Poller(db, FakeQB([])).poll()  # type: ignore[arg-type]

    with db.read() as conn:
        state = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0]
    assert state == "downloading"


def test_completion_stores_content_path(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    Poller(  # type: ignore[arg-type]
        db,
        FakeQB(
            [
                make_qb(
                    "abc",
                    progress=1.0,
                    state="uploading",
                    content_path="/downloads/Movie.2025/Movie.2025.mp4",
                )
            ]
        ),
    ).poll()

    with db.read() as conn:
        row = conn.execute("SELECT content_path FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["content_path"] == "/downloads/Movie.2025/Movie.2025.mp4"


def test_completion_keeps_known_content_path_when_qb_reports_blank(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db,
        sub_id,
        state="downloading",
        qb_hash="abc",
        grabbed_at=to_iso(utc_now()),
        content_path="/downloads/Show.S01E01",
    )
    Poller(  # type: ignore[arg-type]
        db, FakeQB([make_qb("abc", progress=1.0, state="uploading", content_path="")])
    ).poll()

    with db.read() as conn:
        row = conn.execute("SELECT content_path FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["content_path"] == "/downloads/Show.S01E01"


def test_qb_error_state_fails(tmp_path: Path) -> None:
    _, db, sub_id = setup(tmp_path)
    torrent_id = insert_torrent(
        db, sub_id, state="downloading", qb_hash="abc", grabbed_at=to_iso(utc_now())
    )
    Poller(db, FakeQB([make_qb("abc", state="error")])).poll()  # type: ignore[arg-type]

    with db.read() as conn:
        state = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0]
    assert state == "failed"
