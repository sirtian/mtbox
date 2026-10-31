from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from app.clients.qbittorrent import QBTorrent
from app.services import settings_store
from app.services.organizer import Organizer
from app.timeutil import utc_now_iso
from tests.helpers import insert_subscription, insert_torrent, make_db, make_settings

NAME = "Show.S01E01.1080p.WEB-DL.H.264-GROUP"


class FakeQB:
    def __init__(self, content_path: str) -> None:
        self.content_path = content_path

    def info(self, hashes: list[str]) -> list[QBTorrent]:
        return [
            QBTorrent(
                hash=hashes[0],
                name="Movie.2025",
                state="uploading",
                progress=1.0,
                size=1024,
                downloaded=1024,
                save_path="/downloads",
                content_path=self.content_path,
                ratio=0.0,
                seeding_time=0,
                added_on=0,
                completion_on=0,
                category="mtbox",
                tags="mtbox",
            )
        ]


def setup(
    tmp_path: Path,
    *,
    kind: str = "tv",
    title: str = "示例剧",
    episode_from: int | None = 1,
    episode_to: int | None = 1,
    season: int = 1,
    torrent_title: str = NAME,
    episode_start: int | None = 1,
    episode_end: int | None = None,
    files: list[dict[str, object]] | None = None,
    library_category: str | None = None,
    qb_hash: str | None = None,
    content_path: str | None = None,
):
    download = tmp_path / "downloads"
    library = tmp_path / "library"
    settings = make_settings(tmp_path, download, library)
    db = make_db(settings)
    sub_id = insert_subscription(
        db,
        kind=kind,
        title=title,
        season=season,
        episode_from=episode_from,
        episode_to=episode_to,
        library_category=library_category,
    )
    torrent_id = insert_torrent(
        db,
        sub_id,
        title=torrent_title,
        state="completed",
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        files=files or [],
        qb_hash=qb_hash,
        content_path=content_path,
        completed_at=utc_now_iso(),
    )
    return settings, db, sub_id, torrent_id


def write_source(settings, rel: str, content: bytes = b"x" * 1024) -> Path:
    path = settings.download_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_tv_episode_hardlink_and_subtitle(tmp_path: Path) -> None:
    settings, db, sub_id, torrent_id = setup(tmp_path)
    write_source(settings, f"{NAME}/{NAME}.mkv")
    write_source(settings, f"{NAME}/{NAME}.srt", b"[sub]")
    files = [
        {"path": f"{NAME}/{NAME}.mkv", "size": 1024},
        {"path": f"{NAME}/{NAME}.srt", "size": 5},
    ]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "TV" / "示例剧 (2025)" / "Season 01" / "示例剧 - S01E01.mkv"
    subtitle = target.with_suffix(".srt")
    assert target.exists()
    assert target.read_bytes() == b"x" * 1024
    assert subtitle.read_bytes() == b"[sub]"
    assert os.stat(settings.download_dir / files[0]["path"]).st_ino == os.stat(target).st_ino

    with db.read() as conn:
        assert conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0] == "organized"
        assert conn.execute(
            "SELECT state FROM episode_state WHERE subscription_id = ? AND episode = 1", (sub_id,)
        ).fetchone()[0] == "organized"
        assert conn.execute("SELECT status FROM subscription WHERE id = ?", (sub_id,)).fetchone()[0] == "done"


def test_organize_is_idempotent(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": f"{NAME}/{NAME}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    organizer = Organizer(settings, db)
    first = organizer.organize(torrent_id, force=True)
    second = organizer.organize(torrent_id)
    target = settings.library_dir / "TV" / "示例剧 (2025)" / "Season 01" / "示例剧 - S01E01.mkv"

    assert first.ok and second.ok
    assert second.message == "已整理"
    third = organizer.organize(torrent_id, force=True)
    assert third.ok, third.message
    assert target.is_file()
    assert not target.with_name("示例剧 - S01E01 (2).mkv").exists()


def test_existing_conflicting_file_gets_suffix(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": f"{NAME}/{NAME}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    target = settings.library_dir / "TV" / "示例剧 (2025)" / "Season 01" / "示例剧 - S01E01.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    assert target.read_bytes() == b"old"
    assert target.with_name("示例剧 - S01E01 (2).mkv").read_bytes() == b"x" * 1024


def test_exdev_falls_back_to_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": f"{NAME}/{NAME}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    def fail_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr("app.services.organizer.os.link", fail_link)

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "TV" / "示例剧 (2025)" / "Season 01" / "示例剧 - S01E01.mkv"
    assert target.read_bytes() == b"x" * 1024


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path)
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": "../evil.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert not result.ok
    assert "非法文件路径" in result.message
    with db.read() as conn:
        row = conn.execute("SELECT state, error FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "organize_failed"
    assert "非法文件路径" in row["error"]
    assert not (tmp_path / "evil.mkv").exists()


def test_unresolvable_episodes_fail(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(
        tmp_path, episode_start=None, episode_end=None, torrent_title="Show.Complete"
    )
    write_source(settings, "Show.Complete/a.mkv")
    files = [{"path": "Show.Complete/a.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert not result.ok
    assert "无法确定集数" in result.message


def test_batch_range_maps_files_in_natural_order(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(
        tmp_path,
        torrent_title="Show.S01.Complete",
        episode_start=1,
        episode_end=2,
        episode_to=2,
    )
    write_source(settings, "Show.S01.Complete/02.mkv", b"two")
    write_source(settings, "Show.S01.Complete/01.mkv", b"one")
    files = [
        {"path": "Show.S01.Complete/02.mkv", "size": 3},
        {"path": "Show.S01.Complete/01.mkv", "size": 3},
    ]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    season_dir = settings.library_dir / "TV" / "示例剧 (2025)" / "Season 01"
    assert (season_dir / "示例剧 - S01E01.mkv").read_bytes() == b"one"
    assert (season_dir / "示例剧 - S01E02.mkv").read_bytes() == b"two"


def test_movie_template(tmp_path: Path) -> None:
    title = "示例电影"
    settings, db, sub_id, torrent_id = setup(
        tmp_path,
        kind="movie",
        title=title,
        episode_from=None,
        episode_to=None,
        torrent_title="示例电影.2025.1080p.WEB-DL.H.264-GROUP",
        episode_start=None,
    )
    write_source(settings, "movie/示例电影.2025.1080p.mkv")
    files = [{"path": "movie/示例电影.2025.1080p.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "Movies" / "示例电影 (2025)" / "示例电影 (2025) - 1080p.mkv"
    assert target.exists()
    with db.read() as conn:
        assert conn.execute("SELECT status FROM subscription WHERE id = ?", (sub_id,)).fetchone()[0] == "done"


def test_template_uses_subscription_library_category(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path, library_category="动漫")
    settings_store.set_values(
        db,
        {
            "tv_path_template": (
                "{library}/{category}/{title}/Season {season:02d}/"
                "{title} - S{season:02d}E{episode:02d}.{ext}"
            )
        },
    )
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": f"{NAME}/{NAME}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "动漫" / "示例剧" / "Season 01" / "示例剧 - S01E01.mkv"
    assert target.exists()


@pytest.mark.parametrize(
    ("kind", "torrent_title", "episode_start", "expected"),
    [
        ("tv", NAME, 1, "电视剧"),
        ("movie", "示例电影.2025.1080p.WEB-DL.H.264-GROUP", None, "电影"),
    ],
)
def test_template_category_falls_back_by_kind(
    tmp_path: Path,
    kind: str,
    torrent_title: str,
    episode_start: int | None,
    expected: str,
) -> None:
    settings, db, _, torrent_id = setup(
        tmp_path,
        kind=kind,
        title="示例剧",
        episode_from=None if kind == "movie" else 1,
        episode_to=None if kind == "movie" else 1,
        torrent_title=torrent_title,
        episode_start=episode_start,
    )
    key = "movie_path_template" if kind == "movie" else "tv_path_template"
    settings_store.set_values(db, {key: "{library}/{category}/{title}.{ext}"})
    write_source(settings, f"{torrent_title}/{torrent_title}.mkv")
    files = [{"path": f"{torrent_title}/{torrent_title}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    assert (settings.library_dir / expected / "示例剧.mkv").exists()


MOVIE_FILE = "Movie.2025.1080p.WEB-DL.mp4"


def movie_setup(tmp_path: Path, **kwargs):
    return setup(
        tmp_path,
        kind="movie",
        title="示例电影",
        episode_from=None,
        episode_to=None,
        torrent_title="示例电影.2025.1080p.WEB-DL.H.264-GROUP",
        episode_start=None,
        **kwargs,
    )


def test_single_file_wrapped_in_subfolder_is_found(tmp_path: Path) -> None:
    # qBittorrent's "create subfolder" content layout can wrap a single-file
    # torrent in a directory; the file list itself has no folder prefix.
    settings, db, _, torrent_id = movie_setup(
        tmp_path,
        files=[
            {"path": MOVIE_FILE, "size": 1024},
            {"path": "Movie.2025.1080p.WEB-DL.srt", "size": 4},
        ],
    )
    folder = "示例电影.2025.1080p.WEB-DL.H.264-GROUP"
    write_source(settings, f"{folder}/{MOVIE_FILE}")
    write_source(settings, f"{folder}/Movie.2025.1080p.WEB-DL.srt", b"sub!")

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "Movies" / "示例电影 (2025)" / "示例电影 (2025) - 1080p.mp4"
    assert target.read_bytes() == b"x" * 1024
    assert target.with_suffix(".srt").read_bytes() == b"sub!"


def test_stored_content_path_disambiguates_source(tmp_path: Path) -> None:
    settings, db, _, torrent_id = movie_setup(
        tmp_path,
        files=[{"path": MOVIE_FILE, "size": 1024}],
        content_path=f"/downloads/Right/{MOVIE_FILE}",
    )
    write_source(settings, f"Wrong/{MOVIE_FILE}", b"wrong")
    write_source(settings, f"Right/{MOVIE_FILE}", b"right")

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = settings.library_dir / "Movies" / "示例电影 (2025)" / "示例电影 (2025) - 1080p.mp4"
    assert target.read_bytes() == b"right"


def test_live_qb_content_path_wins_over_stored(tmp_path: Path) -> None:
    settings, db, _, torrent_id = movie_setup(
        tmp_path,
        files=[{"path": MOVIE_FILE, "size": 1024}],
        qb_hash="abc",
        content_path=f"/downloads/Gone/{MOVIE_FILE}",
    )
    write_source(settings, f"Wrong/{MOVIE_FILE}", b"wrong")
    write_source(settings, f"Right/{MOVIE_FILE}", b"right")

    qb = FakeQB(f"/downloads/Right/{MOVIE_FILE}")
    result = Organizer(settings, db, qb=qb).organize(torrent_id, force=True)  # type: ignore[arg-type]

    assert result.ok, result.message
    target = settings.library_dir / "Movies" / "示例电影 (2025)" / "示例电影 (2025) - 1080p.mp4"
    assert target.read_bytes() == b"right"


def test_one_entry_multi_torrent_content_path_inside_its_folder(tmp_path: Path) -> None:
    # qB reports the *file* path for a torrent whose "files" list has one entry,
    # even though the file lives inside the torrent folder: <save>/<name>/<name>.ts.
    folder = "The.Shawshank.Redemption.1994.1080i.HDTV.AVS+.DD.2.0-TPTV"
    filename = f"{folder}.ts"
    settings, db, _, torrent_id = setup(
        tmp_path,
        kind="movie",
        title="肖申克的救赎",
        episode_from=None,
        episode_to=None,
        torrent_title=folder,
        episode_start=None,
        files=[{"path": f"{folder}/{filename}", "size": 1024}],
        qb_hash="abc",
        content_path=f"/downloads/{folder}/{filename}",
    )
    write_source(settings, f"{folder}/{filename}")

    qb = FakeQB(f"/downloads/{folder}/{filename}")
    result = Organizer(settings, db, qb=qb).organize(torrent_id, force=True)  # type: ignore[arg-type]

    assert result.ok, result.message
    targets = [
        path
        for path in (settings.library_dir / "Movies").rglob("*.ts")
        if path.is_file()
    ]
    assert len(targets) == 1
    assert targets[0].read_bytes() == b"x" * 1024


def test_actual_qb_content_is_used_when_recorded_file_is_missing(tmp_path: Path) -> None:
    actual = (
        "[肖申克的救赎].The.Shawshank.Redemption.1994.2160p.MAX.WEB-DL.DoVi."
        "HDR10.H265.10bit.DDP5.1.2Audios-UBWEB.mkv"
    )
    settings, db, _, torrent_id = movie_setup(
        tmp_path,
        files=[{"path": "Old.1080p.HDTV.ts", "size": 1}],
        qb_hash="abc",
        content_path=f"/downloads/{actual}",
    )
    write_source(settings, actual, b"real-content")

    qb = FakeQB(f"/downloads/{actual}")
    result = Organizer(settings, db, qb=qb).organize(torrent_id, force=True)  # type: ignore[arg-type]

    assert result.ok, result.message
    targets = [
        path
        for path in (settings.library_dir / "Movies").rglob("*.mkv")
        if path.is_file()
    ]
    assert len(targets) == 1
    assert targets[0].read_bytes() == b"real-content"


def test_default_movie_template_uses_explicit_category(tmp_path: Path) -> None:
    settings, db, _, torrent_id = movie_setup(
        tmp_path,
        library_category="动漫",
        files=[{"path": MOVIE_FILE, "size": 1024}],
    )
    write_source(settings, MOVIE_FILE)

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = (
        settings.library_dir
        / "动漫"
        / "示例电影 (2025)"
        / "示例电影 (2025) - 1080p.mp4"
    )
    assert target.is_file()
    assert not (settings.library_dir / "Movies").exists()


def test_default_tv_template_uses_explicit_category(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(
        tmp_path,
        library_category="动漫",
        files=[{"path": f"{NAME}/{NAME}.mkv", "size": 1024}],
    )
    write_source(settings, f"{NAME}/{NAME}.mkv")

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok, result.message
    target = (
        settings.library_dir
        / "动漫"
        / "示例剧 (2025)"
        / "Season 01"
        / "示例剧 - S01E01.mkv"
    )
    assert target.is_file()
    assert not (settings.library_dir / "TV").exists()


def test_reorganize_moves_links_to_the_new_category(tmp_path: Path) -> None:
    settings, db, sub_id, torrent_id = movie_setup(
        tmp_path, files=[{"path": MOVIE_FILE, "size": 1024}]
    )
    write_source(settings, MOVIE_FILE)
    organizer = Organizer(settings, db)
    first = organizer.organize(torrent_id, force=True)
    assert first.ok, first.message
    old = (
        settings.library_dir
        / "Movies"
        / "示例电影 (2025)"
        / "示例电影 (2025) - 1080p.mp4"
    )
    assert old.is_file()

    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET library_category = '动漫' WHERE id = ?", (sub_id,)
        )
    second = organizer.organize(torrent_id, force=True)

    assert second.ok, second.message
    new = (
        settings.library_dir
        / "动漫"
        / "示例电影 (2025)"
        / "示例电影 (2025) - 1080p.mp4"
    )
    assert new.is_file()
    assert not old.exists()
    with db.read() as conn:
        raw = conn.execute(
            "SELECT organized_paths FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()[0]
    assert [Path(path) for path in json.loads(raw)] == [new]


def test_dry_run_does_not_link(tmp_path: Path) -> None:
    settings, db, _, torrent_id = setup(tmp_path, )
    settings.dry_run = True
    write_source(settings, f"{NAME}/{NAME}.mkv")
    files = [{"path": f"{NAME}/{NAME}.mkv", "size": 1024}]
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET files = ? WHERE id = ?", (json.dumps(files), torrent_id))

    result = Organizer(settings, db).organize(torrent_id, force=True)

    assert result.ok
    assert not (settings.library_dir / "TV").exists()
    with db.read() as conn:
        assert conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()[0] == "completed"
