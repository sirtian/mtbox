from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clients.mteam import MTeamTorrent
from app.services.matcher import Matcher, is_free
from app.timeutil import days_ago_iso, utc_now_iso
from tests.helpers import insert_subscription, insert_torrent, make_db, make_settings


def item(name: str, **overrides: object) -> MTeamTorrent:
    values: dict[str, object] = {
        "id": "1001",
        "name": name,
        "description": "",
        "category": "tv",
        "size_bytes": 2 * 1024**3,
        "file_count": 1,
        "created_at": utc_now_iso(),
        "labels": (),
        "imdb": None,
        "imdb_rating": None,
        "douban": None,
        "douban_rating": None,
        "seeders": 10,
        "leechers": 1,
        "completed": 5,
        "discount": "FREE",
        "discount_end_at": None,
        "visible": True,
        "banned": False,
    }
    values.update(overrides)
    return MTeamTorrent(**values)  # type: ignore[arg-type]


@pytest.fixture
def db(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    return make_db(settings)


def set_quality(db, sub_id: int, quality: dict[str, object]) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET quality = ? WHERE id = ?",
            (json.dumps(quality), sub_id),
        )


def set_episodes(db, sub_id: int, episodes: list[int], state: str = "organized") -> None:
    with db.tx() as conn:
        for episode in episodes:
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
                "VALUES (?, 1, ?, ?, ?)",
                (sub_id, episode, state, utc_now_iso()),
            )


def evaluate(db, sub_id: int, mt_item: MTeamTorrent):
    from app.models import Subscription

    with db.read() as conn:
        row = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    sub = Subscription.from_row(row)
    return Matcher(db).evaluate(sub, mt_item)


def set_episode_info(db, sub_id: int, *, total: int | None, available: int | None) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = ?, available_episode = ? WHERE id = ?",
            (total, available, sub_id),
        )


def grab_plan(db, sub_id: int, *, max_grabs: int = 6) -> list[dict]:
    from app.models import Subscription

    with db.read() as conn:
        sub_row = conn.execute(
            "SELECT * FROM subscription WHERE id = ?", (sub_id,)
        ).fetchone()
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM torrent WHERE subscription_id = ? ORDER BY id", (sub_id,)
            ).fetchall()
        ]
    return Matcher(db).grab_plan(
        Subscription.from_row(sub_row), rows, max_grabs=max_grabs
    )


def test_tv_single_episode_passes(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p.WEB-DL.H.264-GROUP"))
    assert result.passed, result.reason
    assert result.episode_start == 2
    assert result.episode_end is None
    assert result.covered == (2,)
    assert result.score > 0
    assert any(rule.rule == "集数" and rule.passed for rule in result.rules)


def test_episode_offset_is_applied(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025, episode_offset=10)
    result = evaluate(db, sub_id, item("示例剧.S01E01.1080p-G"))
    assert result.passed, result.reason
    assert result.episode_start == 11


def test_movie_rejects_episode_release(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    result = evaluate(db, sub_id, item("示例电影.S01E01.1080p-G"))
    assert not result.passed
    assert result.reason == "类型: 电影订阅不接受剧集种子"


def test_movie_passes(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    result = evaluate(db, sub_id, item("示例电影.2024.1080p.BluRay.x264-GROUP"))
    assert result.passed, result.reason
    assert result.covered == ()
    assert result.episode_start is None


def test_season_mismatch(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025, season=1)
    result = evaluate(db, sub_id, item("示例剧.S02E01.1080p-G"))
    assert not result.passed
    assert "季" in result.reason


def test_imdb_mismatch_is_rejected(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET imdb_id = 'tt111' WHERE id = ?", (sub_id,))
    result = evaluate(
        db, sub_id, item("示例电影.2024.1080p-G", imdb="tt222")
    )
    assert not result.passed
    assert "ID" in result.reason


def test_douban_url_matches_digits_and_skips_title(db) -> None:
    sub_id = insert_subscription(db, title="庆余年", year=2024, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '34937650' WHERE id = ?", (sub_id,)
        )
    result = evaluate(
        db,
        sub_id,
        item(
            "Joy.of.Life.2024.S02E05.2160p.WEB-DL-G",
            douban="https://movie.douban.com/subject/34937650/",
        ),
    )
    assert result.passed, result.reason


def test_douban_url_mismatch_is_rejected(db) -> None:
    sub_id = insert_subscription(db, title="庆余年", year=2024, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '34937650' WHERE id = ?", (sub_id,)
        )
    result = evaluate(
        db,
        sub_id,
        item(
            "庆余年.S02E05.2160p.WEB-DL-G",
            douban="https://movie.douban.com/subject/25853071/",
        ),
    )
    assert not result.passed
    assert "ID" in result.reason


def test_imdb_url_matches_tt_id(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET imdb_id = 'tt11273352' WHERE id = ?", (sub_id,)
        )
    result = evaluate(
        db,
        sub_id,
        item(
            "Example.Movie.2024.1080p-G",
            imdb="https://www.imdb.com/title/tt11273352/",
        ),
    )
    assert result.passed, result.reason


def test_title_mismatch(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(db, sub_id, item("完全不同的一部剧.S01E01.1080p-G"))
    assert not result.passed
    assert "标题" in result.reason


FRIENDS_DESC = "老友记 / 六人行 第二季 / 都市六人行 第二季 | 全24集 | 主演"
GHOSTS_DESC = "古宅老友记 / 同一屋檐吓 / 鬼屋欢乐送 | 全7集 | 主演"


def friends_subscription(db) -> int:
    """A Douban-prefilled subject: per-season title plus the English original."""
    sub_id = insert_subscription(db, title="老友记 第二季", year=1995, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET original_title = ?, aliases = ?, douban_id = ? "
            "WHERE id = ?",
            (
                "Friends Season 2",
                json.dumps(["六人行 第二季", "F·R·I·E·N·D·S"], ensure_ascii=False),
                "3286528",
                sub_id,
            ),
        )
    return sub_id


def test_season_pack_gets_episodes_from_description(db) -> None:
    sub_id = friends_subscription(db)
    result = evaluate(
        db,
        sub_id,
        item("Friends.S02.2160p.WEB-DL.H265-GROUP", description=FRIENDS_DESC),
    )
    assert result.passed, result.reason
    assert result.is_batch is True
    assert result.covered == tuple(range(1, 25))
    assert result.episode_start == 1
    assert result.episode_end == 24


def test_season_only_pack_without_complete_marker_passes(db) -> None:
    # M-Team names many season packs "Friends S02 ..." with no episode number
    # and no "Complete" word; the Sxx marker alone must be enough.
    sub_id = friends_subscription(db)
    result = evaluate(db, sub_id, item("Friends.S02.2160p.WEB-DL-G"))
    assert result.passed, result.reason
    assert result.is_batch is True


def test_other_show_with_embedded_title_is_rejected(db) -> None:
    sub_id = friends_subscription(db)
    result = evaluate(
        db,
        sub_id,
        item("古宅老友记.S02.2160p.WEB-DL-G", description=GHOSTS_DESC),
    )
    assert not result.passed
    assert "标题" in result.reason


def test_unknown_size_pack_rejected_when_season_complete(db) -> None:
    sub_id = friends_subscription(db)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 24 WHERE id = ?", (sub_id,)
        )
        for episode in range(1, 25):
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
                "VALUES (?, 2, ?, 'organized', ?)",
                (sub_id, episode, utc_now_iso()),
            )
    result = evaluate(db, sub_id, item("Friends.S02.2160p.WEB-DL-G"))
    assert not result.passed
    assert "集数" in result.reason


def test_unknown_size_pack_blocks_another_while_downloading(db) -> None:
    sub_id = friends_subscription(db)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO torrent(mt_id, subscription_id, title, size_bytes, state, "
            "is_batch, created_at, updated_at) "
            "VALUES ('900', ?, 'Friends.S02.1080p.BluRay-G', 100, 'downloading', 1, ?, ?)",
            (sub_id, utc_now_iso(), utc_now_iso()),
        )
    result = evaluate(db, sub_id, item("Friends.S02.1080p.WEB-DL-G", id="1002"))
    assert not result.passed
    assert "去重" in result.reason


def test_item_plausible_uses_original_title(db) -> None:
    sub_id = friends_subscription(db)
    from app.models import Subscription

    with db.read() as conn:
        row = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    sub = Subscription.from_row(row)
    matcher = Matcher(db)
    assert matcher.item_plausible(sub, item("Friends.S02.2160p.WEB-DL-G"))
    assert not matcher.item_plausible(sub, item("古宅老友记.S02.2160p.WEB-DL-G"))


def test_year_mismatch(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2020)
    result = evaluate(db, sub_id, item("示例电影.2024.1080p-G"))
    assert not result.passed
    assert "年份" in result.reason


def test_duplicate_episode_is_rejected(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episodes(db, sub_id, [2], state="organized")
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G"))
    assert not result.passed
    assert "E02" in result.reason


def test_duplicate_mt_id_is_rejected(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", id="777"))
    assert result.passed, result.reason
    from tests.helpers import insert_torrent

    insert_torrent(db, sub_id, mt_id="777", state="grabbed", title="示例剧.S01E02.1080p-G")
    second = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", id="777"))
    assert not second.passed
    assert "已存在" in second.reason or "覆盖" in second.reason


def test_batch_below_missing_threshold(db) -> None:
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=10
    )
    set_episodes(db, sub_id, list(range(1, 9)))
    result = evaluate(db, sub_id, item("示例剧.全10集.1080p-G"))
    assert not result.passed
    assert "合集" in result.reason


def test_small_complete_pack_is_kept_when_all_its_episodes_are_missing(db) -> None:
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=10
    )
    result = evaluate(db, sub_id, item("示例剧.S01E7-E8.Complete.1080p-G"))
    assert result.passed, result.reason
    assert result.covered == (7, 8)
    assert result.is_batch


def test_batch_with_enough_missing_passes(db) -> None:
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=10
    )
    set_episodes(db, sub_id, list(range(1, 7)))
    result = evaluate(db, sub_id, item("示例剧.全10集.1080p-G"))
    assert result.passed, result.reason
    assert result.is_batch
    assert result.missing == (7, 8, 9, 10)
    assert result.episode_start == 1
    assert result.episode_end == 10


def test_scope_limits_episodes(db) -> None:
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=5
    )
    result = evaluate(db, sub_id, item("示例剧.S01E09.1080p-G"))
    assert not result.passed
    assert "范围" in result.reason


def test_no_seeders_rejected(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", seeders=0))
    assert not result.passed
    assert "做种" in result.reason


def test_resolution_filter(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_quality(db, sub_id, {"resolutions": ["1080p"]})
    result = evaluate(db, sub_id, item("示例剧.S01E02.720p-G"))
    assert not result.passed
    assert "分辨率" in result.reason


def test_non_free_requires_permission(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_quality(db, sub_id, {"allow_non_free": False})
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", discount="50%"))
    assert not result.passed
    assert "预算" in result.reason


def test_chinese_sub_required(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_quality(db, sub_id, {"require_chinese_sub": True})
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G"))
    assert not result.passed
    assert "中字" in result.reason


def test_chinese_sub_accepts_mteam_label(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_quality(db, sub_id, {"require_chinese_sub": True})
    result = evaluate(
        db,
        sub_id,
        item("示例剧.S01E02.1080p-G", labels=("中字", "中配")),
    )
    assert result.passed, result.reason


def test_old_release_outside_window(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(
        db, sub_id, item("示例剧.S01E02.1080p-G", created_at=days_ago_iso(30))
    )
    assert not result.passed
    assert "追新" in result.reason


def test_window_does_not_apply_with_episode_from(db) -> None:
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=12
    )
    result = evaluate(
        db, sub_id, item("示例剧.S01E02.1080p-G", created_at=days_ago_iso(30))
    )
    assert result.passed, result.reason


def test_known_total_targets_full_season_and_skips_window(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 10 WHERE id = ?", (sub_id,)
        )
    old = item("示例剧.S01E02.1080p-G", created_at=days_ago_iso(30))
    result = evaluate(db, sub_id, old)
    assert result.passed, result.reason
    assert result.covered == (2,)

    beyond = evaluate(db, sub_id, item("示例剧.S01E11.1080p-G"))
    assert not beyond.passed
    assert "范围" in beyond.reason


def test_free_and_resolution_raise_score(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    free_1080 = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", id="1"))
    paid_1080 = evaluate(db, sub_id, item("示例剧.S01E03.1080p-G", id="2", discount="50%"))
    paid_720 = evaluate(db, sub_id, item("示例剧.S01E04.720p-G", id="3", discount="50%"))
    assert free_1080.score > paid_1080.score
    assert paid_1080.score > paid_720.score


def test_4k_outranks_1080p(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    uhd = evaluate(db, sub_id, item("示例剧.S01E02.2160p.WEB-DL-G", id="1"))
    hd = evaluate(db, sub_id, item("示例剧.S01E02.1080p.WEB-DL-G", id="2"))
    assert uhd.passed and hd.passed
    assert uhd.score > hd.score


def test_10bit_outranks_8bit_at_same_resolution(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    ten = evaluate(db, sub_id, item("示例剧.S01E02.1080p.10bit-G", id="1"))
    eight = evaluate(db, sub_id, item("示例剧.S01E02.1080p.8bit-G", id="2"))
    assert ten.passed and eight.passed
    assert ten.score > eight.score


def test_movie_prefers_chinese_dub(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    dubbed = evaluate(
        db, sub_id, item("示例电影.2024.1080p.国语.BluRay-G", id="1")
    )
    plain = evaluate(db, sub_id, item("示例电影.2024.1080p.BluRay-G", id="2"))
    assert dubbed.passed, dubbed.reason
    assert plain.passed, plain.reason
    assert dubbed.score > plain.score


def test_tv_does_not_apply_dub_preference(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    dubbed = evaluate(db, sub_id, item("示例剧.S01E02.1080p.国语-G", id="1"))
    plain = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", id="2"))
    assert dubbed.passed and plain.passed
    assert dubbed.score == plain.score


def test_4k_still_outranks_dubbed_10bit_1080p(db) -> None:
    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    uhd = evaluate(db, sub_id, item("示例电影.2024.2160p.BluRay-G", id="1"))
    hd = evaluate(
        db, sub_id, item("示例电影.2024.1080p.10bit.国语.BluRay-G", id="2")
    )
    assert uhd.passed and hd.passed
    assert uhd.score > hd.score


def test_is_free() -> None:
    assert is_free("FREE")
    assert is_free("2XFREE")
    assert not is_free("2X")
    assert not is_free("50%")
    assert not is_free(None)


def test_guard_paused_blocks_all(db) -> None:
    from app.services.profile_guard import GuardState, save

    save(db, GuardState(mode="paused", ratio=0.4, updated_at=utc_now_iso()))
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    result = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G"))
    assert not result.passed
    assert result.reason.startswith("护栏:")


def test_guard_free_only_allows_free_blocks_paid(db) -> None:
    from app.services.profile_guard import GuardState, save

    save(db, GuardState(mode="free_only", ratio=0.7, updated_at=utc_now_iso()))
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    free = evaluate(db, sub_id, item("示例剧.S01E02.1080p-G", id="1"))
    paid = evaluate(db, sub_id, item("示例剧.S01E03.1080p-G", id="2", discount="50%"))
    assert free.passed, free.reason
    assert not paid.passed
    assert paid.reason.startswith("护栏:")


def test_disk_pressure_blocks_non_free(tmp_path: Path, monkeypatch) -> None:
    from app.services import matcher as matcher_module
    from app.services.storage import DiskUsage

    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    database = make_db(settings)
    monkeypatch.setattr(
        matcher_module,
        "disk_usage",
        lambda _path: DiskUsage(total_gb=100, used_gb=95, free_gb=5, percent=95.0),
    )
    sub_id = insert_subscription(database, title="示例剧", year=2025)
    from app.models import Subscription

    with database.read() as conn:
        row = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    sub = Subscription.from_row(row)
    matcher = Matcher(database, settings)
    free = matcher.evaluate(sub, item("示例剧.S01E02.1080p-G", id="1"))
    paid = matcher.evaluate(sub, item("示例剧.S01E03.1080p-G", id="2", discount="50%"))
    assert free.passed, free.reason
    assert not paid.passed
    assert paid.reason.startswith("磁盘:")


def test_movie_with_active_task_rejects_other_releases(db) -> None:
    from tests.helpers import insert_torrent

    sub_id = insert_subscription(db, kind="movie", title="示例电影", year=2024)
    insert_torrent(
        db,
        sub_id,
        mt_id="900",
        title="示例电影.2024.1080p.BluRay-GROUP",
        state="grabbed",
        episode_start=None,
    )
    result = evaluate(db, sub_id, item("示例电影.2024.2160p.WEB-DL-GROUP", id="901"))
    assert not result.passed
    assert "电影" in result.reason


def test_best_candidates_prefers_missing_episode_coverage(db) -> None:
    from tests.helpers import insert_torrent

    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=10
    )
    single = insert_torrent(
        db,
        sub_id,
        mt_id="single",
        title="示例剧.S01E10.1080p-GROUP",
        episode_start=10,
    )
    batch = insert_torrent(
        db,
        sub_id,
        mt_id="batch",
        title="示例剧.全10集.1080p-GROUP",
        episode_start=1,
        episode_end=10,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 90 WHERE id = ?", (single,))
        conn.execute("UPDATE torrent SET score = 60 WHERE id = ?", (batch,))

    rows = Matcher(db).best_candidates()
    assert len(rows) == 1
    assert rows[0]["id"] == batch
    assert rows[0]["missing_count"] == 10
    assert rows[0]["alt_count"] == 1


def test_best_candidates_returns_one_row_per_subscription(db) -> None:
    from tests.helpers import insert_torrent

    first = insert_subscription(db, title="剧一", year=2025)
    second = insert_subscription(db, title="剧二", year=2025)
    insert_torrent(db, first, mt_id="a1", title="剧一.S01E01.1080p-G", episode_start=1)
    insert_torrent(db, first, mt_id="a2", title="剧一.S01E02.1080p-G", episode_start=2)
    insert_torrent(db, second, mt_id="b1", title="剧二.S01E01.1080p-G", episode_start=1)
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 80 WHERE mt_id = 'a1'")
        conn.execute("UPDATE torrent SET score = 70 WHERE mt_id = 'a2'")
        conn.execute("UPDATE torrent SET score = 75 WHERE mt_id = 'b1'")

    rows = Matcher(db).best_candidates()
    assert len(rows) == 2
    by_sub = {row["subscription_id"]: row for row in rows}
    assert by_sub[first]["mt_id"] == "a1"
    assert by_sub[second]["mt_id"] == "b1"


def test_update_to_pack_covers_all_aired_episodes(db) -> None:
    sub_id = insert_subscription(db, title="嫁金钗", year=2025)
    set_episode_info(db, sub_id, total=36, available=14)
    result = evaluate(
        db,
        sub_id,
        item("嫁金钗.S01.更新至14集.2160p.WEB-DL-G"),
    )
    assert result.passed, result.reason
    assert result.covered == tuple(range(1, 15))
    assert result.is_batch is True


def test_grab_plan_collects_single_episodes_for_every_aired_episode(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=6, available=4)
    ids = [
        insert_torrent(
            db,
            sub_id,
            mt_id=f"e{episode}",
            title=f"示例剧.S01E{episode:02d}.1080p-G",
            episode_start=episode,
        )
        for episode in range(1, 5)
    ]
    with db.tx() as conn:
        for score, torrent_id in zip((90, 80, 70, 60), ids):
            conn.execute("UPDATE torrent SET score = ? WHERE id = ?", (score, torrent_id))

    plan = grab_plan(db, sub_id)
    assert {row["id"] for row in plan} == set(ids)


def test_grab_plan_prefers_one_cumulative_pack_over_partial_episodes(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=10, available=6)
    single = insert_torrent(
        db,
        sub_id,
        mt_id="single",
        title="示例剧.S01E06.2160p-G",
        episode_start=6,
    )
    pack = insert_torrent(
        db,
        sub_id,
        mt_id="pack",
        title="示例剧.S01.更新至6集.1080p-G",
        episode_start=1,
        episode_end=6,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 95 WHERE id = ?", (single,))
        conn.execute("UPDATE torrent SET score = 55 WHERE id = ?", (pack,))

    plan = grab_plan(db, sub_id)
    assert [row["id"] for row in plan] == [pack]


def test_grab_plan_respects_grab_cap(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=12, available=8)
    for episode in range(1, 9):
        insert_torrent(
            db,
            sub_id,
            mt_id=f"e{episode}",
            title=f"示例剧.S01E{episode:02d}.1080p-G",
            episode_start=episode,
        )

    plan = grab_plan(db, sub_id, max_grabs=3)
    assert len(plan) == 3


def test_grab_plan_stops_at_daily_budget(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=4, available=2)
    for episode in (1, 2):
        torrent_id = insert_torrent(
            db,
            sub_id,
            mt_id=f"e{episode}",
            title=f"示例剧.S01E{episode:02d}.1080p-G",
            episode_start=episode,
            size_bytes=3 * 1024**3,
        )
        with db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET discount = 'NORMAL', score = 80 WHERE id = ?",
                (torrent_id,),
            )
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO setting(key, value) VALUES ('daily_download_budget_gb', '5') "
            "ON CONFLICT(key) DO UPDATE SET value = '5'"
        )

    plan = grab_plan(db, sub_id)
    assert len(plan) == 1


def test_unknown_size_pack_does_not_outrank_known_coverage(db) -> None:
    from app.models import Subscription

    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=10, available=6)
    single = insert_torrent(
        db,
        sub_id,
        mt_id="single",
        title="示例剧.S01E06.2160p-G",
        episode_start=6,
    )
    pack = insert_torrent(
        db,
        sub_id,
        mt_id="pack",
        title="示例剧.S01.2160p-G",
        episode_start=None,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 95 WHERE id = ?", (single,))
        conn.execute("UPDATE torrent SET score = 60, is_batch = 1 WHERE id = ?", (pack,))
        sub_row = conn.execute(
            "SELECT * FROM subscription WHERE id = ?", (sub_id,)
        ).fetchone()
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM torrent WHERE subscription_id = ?", (sub_id,)
            ).fetchall()
        ]

    ranked = Matcher(db).rank_candidates(Subscription.from_row(sub_row), rows)
    # An uploader can label one episode "S01 Complete"; unknown coverage must
    # not hide a seed whose episodes are known.
    assert ranked[0]["id"] == single
    assert ranked[-1]["id"] == pack
    assert ranked[-1]["coverage_unknown"] is True
    assert ranked[-1]["missing_count"] == 6  # aired episodes only, not total 10


def test_best_candidates_plans_all_aired_episodes_despite_fake_complete_pack(db) -> None:
    """Regression for 嫁金钗: a fake S01 Complete must not collapse the plan."""
    sub_id = insert_subscription(db, title="嫁金钗", year=2026)
    set_episode_info(db, sub_id, total=30, available=20)
    ranges = [(1, 6), (7, 8), (9, 10), (11, 16), (17, 18), (19, 20)]
    ids = {
        couple: insert_torrent(
            db,
            sub_id,
            mt_id=f"pack-{couple[0]}",
            title=(
                f"嫁金钗.S01E{couple[0]:02d}-E{couple[1]:02d}.2160p.WEB-DL-G"
            ),
            episode_start=couple[0],
            episode_end=couple[1],
        )
        for couple in ranges
    }
    fake = insert_torrent(
        db,
        sub_id,
        mt_id="fake-complete",
        title="嫁金钗.S01.Complete.2160p.WEB-DL-G",
        episode_start=None,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET is_batch = 1, score = 99 WHERE id = ?", (fake,))
        for score, torrent_id in zip((80, 79, 78, 77, 76, 75), ids.values()):
            conn.execute("UPDATE torrent SET score = ? WHERE id = ?", (score, torrent_id))

    rows = Matcher(db).best_candidates()
    assert {row["id"] for row in rows} == set(ids.values())
    assert fake not in {row["id"] for row in rows}
    assert {row["plan_count"] for row in rows} == {6}
    covered: set[int] = set()
    for row in rows:
        start = row["episode_start"]
        end = row["episode_end"] or start
        covered.update(range(start, end + 1))
        assert row["missing_count"] <= 20
    assert covered == set(range(1, 21))


def test_grab_plan_uses_unknown_pack_only_as_fallback(db) -> None:
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=6, available=4)
    known = insert_torrent(
        db,
        sub_id,
        mt_id="known",
        title="示例剧.S01E01.2160p-G",
        episode_start=1,
    )
    pack = insert_torrent(
        db,
        sub_id,
        mt_id="pack",
        title="示例剧.S01.Complete.2160p-G",
        episode_start=None,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET is_batch = 1, score = 90 WHERE id = ?", (pack,))
        conn.execute("UPDATE torrent SET score = 50 WHERE id = ?", (known,))

    plan = grab_plan(db, sub_id)
    # Known coverage is taken first; the unverifiable pack fills what is left.
    assert [row["id"] for row in plan] == [known, pack]
