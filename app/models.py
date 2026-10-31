from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


def _json_list(value: object) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class Subscription:
    id: int
    kind: str
    title: str
    original_title: str | None
    aliases: list[str]
    year: int | None
    douban_id: str | None
    imdb_id: str | None
    season: int
    episode_offset: int
    episode_from: int | None
    episode_to: int | None
    quality: dict[str, Any]
    rules: dict[str, Any]
    auto_grab: bool
    interval_min: int
    status: str
    deleted_at: str | None
    last_checked_at: str | None
    last_error: str | None
    error_count: int
    created_at: str
    updated_at: str
    total_episodes: int | None = None
    available_episode: int | None = None
    episodes_source: str | None = None
    library_category: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Subscription":
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            original_title=row["original_title"],
            aliases=[str(item) for item in _json_list(row["aliases"])],
            year=row["year"],
            douban_id=row["douban_id"],
            imdb_id=row["imdb_id"],
            season=row["season"],
            episode_offset=row["episode_offset"],
            episode_from=row["episode_from"],
            episode_to=row["episode_to"],
            quality=_json_dict(row["quality"]),
            rules=_json_dict(row["rules"]),
            auto_grab=bool(row["auto_grab"]),
            interval_min=row["interval_min"],
            status=row["status"],
            deleted_at=row["deleted_at"],
            last_checked_at=row["last_checked_at"],
            last_error=row["last_error"],
            error_count=row["error_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            total_episodes=row["total_episodes"],
            available_episode=row["available_episode"],
            episodes_source=row["episodes_source"],
            library_category=row["library_category"],
        )


@dataclass(slots=True)
class Torrent:
    id: int
    mt_id: str
    subscription_id: int
    title: str
    size_bytes: int
    discount: str | None
    seeders: int
    leechers: int
    category: str | None
    publish_at: str | None
    season: int | None
    episode_start: int | None
    episode_end: int | None
    is_batch: bool
    score: float | None
    reasons: list[dict[str, Any]]
    state: str
    ignored_by: str | None
    qb_hash: str | None
    save_path: str | None
    content_path: str | None
    files: list[dict[str, Any]]
    progress: float
    grabbed_at: str | None
    completed_at: str | None
    organized_at: str | None
    organized_paths: list[str]
    seed_started_at: str | None
    deleted_at: str | None
    error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Torrent":
        return cls(
            id=row["id"],
            mt_id=row["mt_id"],
            subscription_id=row["subscription_id"],
            title=row["title"],
            size_bytes=row["size_bytes"],
            discount=row["discount"],
            seeders=row["seeders"],
            leechers=row["leechers"],
            category=row["category"],
            publish_at=row["publish_at"],
            season=row["season"],
            episode_start=row["episode_start"],
            episode_end=row["episode_end"],
            is_batch=bool(row["is_batch"]),
            score=row["score"],
            reasons=_json_list(row["reasons"]),
            state=row["state"],
            ignored_by=row["ignored_by"],
            qb_hash=row["qb_hash"],
            save_path=row["save_path"],
            content_path=row["content_path"],
            files=_json_list(row["files"]),
            progress=row["progress"],
            grabbed_at=row["grabbed_at"],
            completed_at=row["completed_at"],
            organized_at=row["organized_at"],
            organized_paths=_json_list(row["organized_paths"]),
            seed_started_at=row["seed_started_at"],
            deleted_at=row["deleted_at"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def covered_episodes(self) -> list[int]:
        if self.episode_start is None:
            return []
        if self.episode_end is None or self.episode_end == self.episode_start:
            return [self.episode_start]
        if self.episode_end < self.episode_start:
            return []
        return list(range(self.episode_start, self.episode_end + 1))


@dataclass(slots=True)
class EpisodeState:
    subscription_id: int
    season: int
    episode: int
    torrent_id: int | None
    state: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EpisodeState":
        return cls(
            subscription_id=row["subscription_id"],
            season=row["season"],
            episode=row["episode"],
            torrent_id=row["torrent_id"],
            state=row["state"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class AccountSnapshot:
    ratio: float | None
    uploaded: int | None
    downloaded: int | None
    bonus: float | None
    captured_at: str


@dataclass(slots=True)
class Progress:
    subscription_id: int
    kind: str
    organized: int
    in_flight: int
    total_target: int | None = field(default=None)
    available: int | None = field(default=None)
    waiting: int | None = field(default=None)
    unaired: int | None = field(default=None)
    episodes_source: str | None = field(default=None)
    # 电影没有 episode_state，进度状态取最近一次实际任务（torrent）的状态。
    movie_state: str | None = field(default=None)

    @property
    def downloaded(self) -> int:
        return self.organized + self.in_flight

    @property
    def percent(self) -> float | None:
        base = self.total_target or self.available
        if not base:
            return None
        return min(100.0, round(self.downloaded * 100.0 / base, 1))
