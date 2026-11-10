from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.services import settings_store
from app.timeutil import utc_now_iso


def bencode(value: object) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode("utf-8"))
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda pair: str(pair[0]))
        return b"d" + b"".join(
            bencode(key) + bencode(item) for key, item in items  # type: ignore[arg-type]
        ) + b"e"
    raise TypeError(type(value))


def make_torrent(name: str, files: list[tuple[str, int]], *, multi: bool) -> bytes:
    if multi:
        info: dict[object, object] = {
            "name": name,
            "files": [
                {"length": size, "path": path.split("/")} for path, size in files
            ],
        }
    else:
        info = {"name": name, "length": files[0][1]}
    return bencode({"announce": "https://tracker.invalid/announce", "info": info})


def make_settings(tmp_path: Path, download: Path, library: Path, **overrides: object) -> Settings:
    data = tmp_path / "config"
    data.mkdir(parents=True, exist_ok=True)
    download.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "mt_api_key": "test-key",
        "download_dir": download,
        "library_dir": library,
        "data_dir": data,
        "path_map_json": json.dumps([["/downloads", str(download)]]),
        "qb_save_path": "/downloads",
        "qb_category": "mtbox",
        "qb_tags": "mtbox",
        "min_video_size_mb": 0,
        "organize_delay_sec": 0,
        "dry_run": False,
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.init()
    settings_store.ensure_defaults(database)
    return database


def insert_subscription(
    db: Database,
    *,
    kind: str = "tv",
    title: str = "示例剧",
    season: int = 1,
    episode_from: int | None = None,
    episode_to: int | None = None,
    year: int | None = 2025,
    douban_id: str | None = None,
    episode_offset: int = 0,
    status: str = "active",
    library_category: str | None = None,
    total_episodes: int | None = None,
) -> int:
    now = utc_now_iso()
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO subscription(kind, title, year, douban_id, season, "
            "episode_offset, episode_from, episode_to, total_episodes, status, "
            "library_category, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                title,
                year,
                douban_id,
                season,
                episode_offset,
                episode_from,
                episode_to,
                total_episodes,
                status,
                library_category,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def insert_torrent(
    db: Database,
    subscription_id: int,
    *,
    mt_id: str = "1001",
    title: str = "示例剧.S01E01.1080p.WEB-DL.H.264-GROUP",
    state: str = "candidate",
    season: int | None = 1,
    episode_start: int | None = 1,
    episode_end: int | None = None,
    size_bytes: int = 1_000_000_000,
    qb_hash: str | None = None,
    save_path: str | None = "/downloads",
    content_path: str | None = None,
    files: list[dict[str, object]] | None = None,
    grabbed_at: str | None = None,
    completed_at: str | None = None,
) -> int:
    now = utc_now_iso()
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO torrent(mt_id, subscription_id, title, size_bytes, season, "
            "episode_start, episode_end, state, qb_hash, save_path, content_path, files, "
            "grabbed_at, completed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mt_id,
                subscription_id,
                title,
                size_bytes,
                season,
                episode_start,
                episode_end,
                state,
                qb_hash,
                save_path,
                content_path,
                json.dumps(files or [], ensure_ascii=False),
                grabbed_at,
                completed_at,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)
