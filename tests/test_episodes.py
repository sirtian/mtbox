from __future__ import annotations

from pathlib import Path

from app.clients.mteam import MTeamTorrent
from app.clients.release_name import parse_release
from app.services.episodes import (
    extract_available,
    extract_total,
    metadata_from_items,
)
from app.services.subscriptions import SubscriptionService
from app.timeutil import utc_now_iso
from tests.helpers import insert_subscription, make_db, make_settings


def item(
    name: str,
    *,
    description: str = "",
    douban: str | None = None,
    imdb: str | None = None,
) -> MTeamTorrent:
    return MTeamTorrent(
        id="1",
        name=name,
        description=description,
        category="402",
        size_bytes=1024**3,
        file_count=1,
        created_at=utc_now_iso(),
        labels=(),
        imdb=imdb,
        imdb_rating=None,
        douban=douban,
        douban_rating=None,
        seeders=10,
        leechers=1,
        completed=0,
        discount="FREE",
        discount_end_at=None,
        visible=True,
        banned=False,
    )


def subscription(
    tmp_path: Path,
    *,
    douban_id: str | None = None,
    season: int = 1,
    offset: int = 0,
):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    sub_id = insert_subscription(
        db, title="示例剧", year=2026, season=season, episode_offset=offset
    )
    if douban_id:
        with db.tx() as conn:
            conn.execute(
                "UPDATE subscription SET douban_id = ? WHERE id = ?", (douban_id, sub_id)
            )
    return db, SubscriptionService(db).get(sub_id)


def test_extract_total_from_common_descriptions() -> None:
    assert extract_total("示例剧 | 全32集 | 主演") == 32
    assert extract_total("兰香如故 47集全") == 47
    assert extract_total("全 3 话") == 3
    assert extract_total("共12期") == 12
    assert extract_total("第13-14集") is None
    assert extract_total("S01E05") is None


def test_extract_available_from_descriptions() -> None:
    assert extract_available("小城良方 | 第13-14集") == 14
    assert extract_available("第1季第5集") == 5
    assert extract_available("更新至14集") == 14
    assert extract_available("全32集") is None
    assert extract_available("2026 / 中国大陆") is None


def test_complete_pack_gives_total_and_available(tmp_path: Path) -> None:
    _, sub = subscription(tmp_path)
    meta = metadata_from_items(
        sub,
        [
            item(
                "示例剧.S01.Complete.1080p.WEB-DL-GROUP",
                description="示例剧 | 全32集 | 主演",
            )
        ],
    )
    assert meta.total == 32
    assert meta.available == 32
    assert parse_release("示例剧.S01.Complete.1080p.WEB-DL-GROUP").is_batch is True


def test_item_ignored_when_douban_id_differs(tmp_path: Path) -> None:
    _, sub = subscription(tmp_path, douban_id="111111")
    meta = metadata_from_items(
        sub,
        [
            item(
                "示例剧.S02E05.1080p-GROUP",
                description="示例剧 第二季 全24集",
                douban="https://movie.douban.com/subject/222222/",
            )
        ],
    )
    assert meta.total is None
    assert meta.available is None


def test_item_from_other_season_is_ignored(tmp_path: Path) -> None:
    _, sub = subscription(tmp_path, season=2)
    meta = metadata_from_items(
        sub,
        [
            item(
                "示例剧.S01.Complete.1080p-GROUP",
                description="示例剧 第一季 全46集",
            )
        ],
    )
    assert meta.total is None


def test_url_ids_match_and_offset_is_applied(tmp_path: Path) -> None:
    _, sub = subscription(
        tmp_path, douban_id="34937650", season=2, offset=-12
    )
    meta = metadata_from_items(
        sub,
        [
            item(
                "示例剧.S02E13-E14.1080p-GROUP",
                description="示例剧 第二季 | 第13-14集",
                douban="https://movie.douban.com/subject/34937650/",
                imdb="https://www.imdb.com/title/tt11273352/",
            )
        ],
    )
    assert meta.total is None
    assert meta.available == 2


def test_available_is_capped_to_total(tmp_path: Path) -> None:
    _, sub = subscription(tmp_path)
    meta = metadata_from_items(
        sub,
        [item("示例剧.S01E05.1080p-GROUP", description="示例剧 全3集")],
    )
    assert meta.total == 3
    assert meta.available == 3
