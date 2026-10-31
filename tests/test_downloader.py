from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clients.bencode import parse_torrent
from app.clients.qbittorrent import QBTorrent
from app.errors import Conflict, QBError
from app.services.downloader import (
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    OUTCOME_GRABBED,
    OUTCOME_SKIPPED,
    DownloadService,
)
from tests.helpers import (
    insert_subscription,
    insert_torrent,
    make_db,
    make_settings,
    make_torrent,
)

NAME = "Show.S01E01.1080p.WEB-DL.H.264-GROUP"
TORRENT_BYTES = make_torrent(
    NAME, [(f"{NAME}.mkv", 900), (f"{NAME}.srt", 10)], multi=True
)
INFO_HASH = parse_torrent(TORRENT_BYTES).info_hash


class FakeMT:
    def __init__(self) -> None:
        self.calls = 0
        self.download_url_calls = 0

    def gen_download_url(self, mt_id: str) -> str:
        self.download_url_calls += 1
        return f"https://dl.invalid/{mt_id}.torrent"

    def download_torrent(self, url: str) -> bytes:
        self.calls += 1
        return TORRENT_BYTES


class FakeQB:
    def __init__(self, *, existing: bool = False, fail_add: bool = False) -> None:
        self.existing = existing
        self.fail_add = fail_add
        self.added: list[dict[str, object]] = []
        self.category: str | None = None

    def ensure_category(self, name: str) -> None:
        self.category = name

    def info(self, hashes: list[str]) -> list[QBTorrent]:
        if not self.existing or not any(item == INFO_HASH for item in hashes):
            return []
        return [
            QBTorrent(
                hash=INFO_HASH,
                name=NAME,
                state="downloading",
                progress=0.0,
                size=910,
                downloaded=0,
                save_path="/downloads",
                content_path="",
                ratio=0.0,
                seeding_time=0,
                added_on=0,
                completion_on=0,
                category="mtbox",
                tags="mtbox",
            )
        ]

    def add(self, torrent: bytes, **kwargs: object) -> None:
        if self.fail_add:
            raise QBError("qB 拒绝任务")
        self.added.append({"bytes": len(torrent), **kwargs})


def setup(tmp_path: Path, **settings_overrides: object):
    download = tmp_path / "downloads"
    library = tmp_path / "library"
    settings = make_settings(tmp_path, download, library, **settings_overrides)
    db = make_db(settings)
    sub_id = insert_subscription(db)
    torrent_id = insert_torrent(db, sub_id)
    return settings, db, sub_id, torrent_id


def test_grab_success(tmp_path: Path) -> None:
    settings, db, sub_id, torrent_id = setup(tmp_path)
    mt, qb = FakeMT(), FakeQB()

    result = DownloadService(settings, db, mt, qb).grab(torrent_id)  # type: ignore[arg-type]

    assert result.outcome == OUTCOME_GRABBED
    with db.read() as conn:
        row = conn.execute("SELECT * FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
        episodes = conn.execute(
            "SELECT season, episode, state FROM episode_state WHERE subscription_id = ?",
            (sub_id,),
        ).fetchall()
    assert row["state"] == "downloading"
    assert row["qb_hash"] == INFO_HASH
    assert row["save_path"] == "/downloads"
    files = json.loads(row["files"])
    assert files[0]["path"] == f"{NAME}/{NAME}.mkv"
    assert [dict(item) for item in episodes] == [
        {"season": 1, "episode": 1, "state": "grabbed"}
    ]
    assert qb.category == "mtbox"
    assert qb.added and qb.added[0]["save_path"] == "/downloads"
    assert qb.added[0]["seeding_time_limit_min"] == 72 * 60
    assert (settings.torrents_dir / "1001.torrent").read_bytes() == TORRENT_BYTES


def test_dry_run_does_not_mutate(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path, dry_run=True)
    mt, qb = FakeMT(), FakeQB()

    result = DownloadService(settings, db, mt, qb).grab(torrent_id)  # type: ignore[arg-type]

    assert result.outcome == OUTCOME_DRY_RUN
    with db.read() as conn:
        row = conn.execute("SELECT state, qb_hash FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "candidate"
    assert row["qb_hash"] is None
    assert mt.download_url_calls == 0
    assert qb.added == []


def test_second_grab_is_skipped(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    service = DownloadService(settings, db, FakeMT(), FakeQB())  # type: ignore[arg-type]
    service.grab(torrent_id)

    result = service.grab(torrent_id)
    assert result.outcome == OUTCOME_SKIPPED


def test_duplicate_episode_is_conflict(tmp_path: Path) -> None:
    settings, db, sub_id, torrent_id = setup(tmp_path)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, NULL, 'organized', '2026-01-01T00:00:00Z')",
            (sub_id,),
        )
    mt, qb = FakeMT(), FakeQB()

    with pytest.raises(Conflict):
        DownloadService(settings, db, mt, qb).grab(torrent_id)  # type: ignore[arg-type]

    with db.read() as conn:
        state = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0]
    assert state == "candidate"
    assert mt.download_url_calls == 0


def test_qb_failure_marks_failed(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    qb = FakeQB(fail_add=True)

    result = DownloadService(settings, db, FakeMT(), qb).grab(torrent_id)  # type: ignore[arg-type]

    assert result.outcome == OUTCOME_FAILED
    with db.read() as conn:
        row = conn.execute("SELECT state, error FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "failed"
    assert "qB" in row["error"]


def test_existing_qb_torrent_is_adopted(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    qb = FakeQB(existing=True)

    result = DownloadService(settings, db, FakeMT(), qb).grab(torrent_id)  # type: ignore[arg-type]

    assert result.outcome == OUTCOME_GRABBED
    assert qb.added == []
    with db.read() as conn:
        state = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0]
    assert state == "downloading"


def test_batch_only_writes_missing_episodes(tmp_path: Path) -> None:
    download, library = tmp_path / "downloads", tmp_path / "library"
    settings = make_settings(tmp_path, download, library)
    db = make_db(settings)
    sub_id = insert_subscription(db)
    torrent_id = insert_torrent(
        db, sub_id, mt_id="2002", episode_start=1, episode_end=3, title="Show.S01.Complete"
    )
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
            "VALUES (?, 1, 1, NULL, 'organized', '2026-01-01T00:00:00Z')",
            (sub_id,),
        )

    result = DownloadService(settings, db, FakeMT(), FakeQB()).grab(torrent_id)  # type: ignore[arg-type]

    assert result.outcome == OUTCOME_GRABBED
    with db.read() as conn:
        rows = conn.execute(
            "SELECT episode, state FROM episode_state WHERE subscription_id = ? ORDER BY episode",
            (sub_id,),
        ).fetchall()
    assert [(row["episode"], row["state"]) for row in rows] == [
        (1, "organized"),
        (2, "grabbed"),
        (3, "grabbed"),
    ]


def set_discount(db, torrent_id: int, discount: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE torrent SET discount = ? WHERE id = ?", (discount, torrent_id)
        )


def test_grab_refused_when_guard_paused(tmp_path: Path) -> None:
    from app.services.profile_guard import GuardState, save

    settings, db, _, torrent_id = setup(tmp_path)
    save(db, GuardState(mode="paused", ratio=0.4, updated_at="2026-01-01T00:00:00Z"))
    qb = FakeQB()
    with pytest.raises(Conflict, match="暂停"):
        DownloadService(settings, db, FakeMT(), qb).grab(torrent_id)  # type: ignore[arg-type]
    assert qb.added == []
    assert FakeMT().calls == 0


def test_grab_free_only_refuses_paid_torrent(tmp_path: Path) -> None:
    from app.services.profile_guard import GuardState, save

    settings, db, _, torrent_id = setup(tmp_path)
    set_discount(db, torrent_id, "50%")
    save(db, GuardState(mode="free_only", ratio=0.7, updated_at="2026-01-01T00:00:00Z"))
    qb = FakeQB()
    with pytest.raises(Conflict, match="仅允许免费"):
        DownloadService(settings, db, FakeMT(), qb).grab(torrent_id)  # type: ignore[arg-type]
    assert qb.added == []


def test_grab_disk_pressure_refuses_paid_torrent(tmp_path: Path, monkeypatch) -> None:
    from app.services import downloader as downloader_module
    from app.services.storage import DiskUsage

    settings, db, _, torrent_id = setup(tmp_path)
    set_discount(db, torrent_id, "50%")
    monkeypatch.setattr(
        downloader_module,
        "disk_usage",
        lambda _path: DiskUsage(total_gb=100, used_gb=95, free_gb=5, percent=95.0),
    )
    qb = FakeQB()
    with pytest.raises(Conflict, match="磁盘水位"):
        DownloadService(settings, db, FakeMT(), qb).grab(torrent_id)  # type: ignore[arg-type]
    assert qb.added == []


def test_grab_success_notifies(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)

    class FakeNotifier:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str, str]] = []

        def send(self, title: str, message: str, *, level: str = "info"):
            self.messages.append((title, message, level))
            return ("fake",)

    notifier = FakeNotifier()
    result = DownloadService(settings, db, FakeMT(), FakeQB(), notifier=notifier).grab(  # type: ignore[arg-type]
        torrent_id
    )
    assert result.outcome == OUTCOME_GRABBED
    assert len(notifier.messages) == 1
    assert "抓取成功" in notifier.messages[0][0]


def test_file_entries_single_file_has_no_folder() -> None:
    meta = parse_torrent(make_torrent("Movie.2023.mkv", [("Movie.2023.mkv", 100)], multi=False))
    assert DownloadService._file_entries(meta) == [
        {"path": "Movie.2023.mkv", "size": 100}
    ]


def test_file_entries_keep_folder_for_single_entry_multi_torrent() -> None:
    # qBittorrent still creates the torrent folder for a one-file "files" list.
    meta = parse_torrent(make_torrent("Movie.2023", [("Movie.2023.mkv", 100)], multi=True))
    assert DownloadService._file_entries(meta) == [
        {"path": "Movie.2023/Movie.2023.mkv", "size": 100}
    ]
