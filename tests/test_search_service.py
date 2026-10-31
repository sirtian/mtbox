from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.clients.douban import DoubanEpisodes
from app.clients.mteam import MTeamSearchResult, MTeamTorrent
from app.errors import MTeamApiError, MTeamError, RateLimitBusy
from app.services import settings_store
from app.services.downloader import GrabResult
from app.services.matcher import Matcher
from app.services.search_service import SearchService
from app.timeutil import to_iso, utc_now, utc_now_iso
from tests.helpers import insert_subscription, make_db, make_settings, insert_torrent


def torrent(mt_id: str = "101", name: str = "示例剧.S01E02.1080p.WEB-DL.H.264-GROUP", **overrides: object) -> MTeamTorrent:
    values: dict[str, object] = {
        "id": mt_id,
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
        "completed": 2,
        "discount": "FREE",
        "discount_end_at": None,
        "visible": True,
        "banned": False,
    }
    values.update(overrides)
    return MTeamTorrent(**values)  # type: ignore[arg-type]


def result(*items: MTeamTorrent) -> MTeamSearchResult:
    return MTeamSearchResult("示例剧", tuple(items), len(items), 1, 100)


class FakeMT:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def search(self, keyword: str, **kwargs: object) -> MTeamSearchResult:
        self.calls.append((keyword, kwargs))
        if not self.responses:
            return result()
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


class FakeDownloader:
    def __init__(self, outcome: str = "grabbed") -> None:
        self.outcome = outcome
        self.called: list[int] = []

    def grab(self, torrent_id: int, *, actor: str) -> GrabResult:
        self.called.append(torrent_id)
        return GrabResult(torrent_id, self.outcome, "已加入下载")


class FakeDouban:
    def __init__(
        self,
        total: int | None,
        available: int | None = None,
        *,
        original_title: str = "",
        aliases: tuple[str, ...] = (),
        year: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.episodes = DoubanEpisodes(
            subject_id="34937650",
            total=total,
            available=available,
            info="",
            fetched_at=utc_now_iso(),
            original_title=original_title,
            aliases=aliases,
            year=year,
        )

    def fetch_episodes(self, subject_id: str, *, refresh: bool = False) -> DoubanEpisodes:
        self.calls.append(subject_id)
        return self.episodes


@pytest.fixture
def ctx(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    return settings, db


def service(settings, db, mt, downloader=None, douban=None) -> SearchService:
    return SearchService(settings, db, mt, downloader, douban=douban)  # type: ignore[arg-type]


def row_for(db, mt_id: str):
    with db.read() as conn:
        return conn.execute("SELECT * FROM torrent WHERE mt_id = ?", (mt_id,)).fetchone()


def test_check_stores_candidate(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(result(torrent()))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert summary.searched == 1
    assert summary.candidates == 1
    row = row_for(db, "101")
    assert row["state"] == "candidate"
    assert row["episode_start"] == 2
    assert row["score"] and row["score"] > 0
    reasons = json.loads(row["reasons"])
    assert any(item["rule"] == "集数" for item in reasons)
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["last_checked_at"] is not None
    assert sub["error_count"] == 0


def test_check_is_idempotent(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(result(torrent()), result(torrent()))
    svc = service(settings, db, mt)
    svc.check_subscription(sub_id, manual=True)
    svc.check_subscription(sub_id, manual=True)
    with db.read() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM torrent").fetchone()["n"]
    assert count == 1


def test_low_score_is_ignored_then_revived(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    settings_store.set_values(db, {"min_score": "100"})
    mt = FakeMT(result(torrent()), result(torrent()))
    svc = service(settings, db, mt)
    first = svc.check_subscription(sub_id, manual=True)
    assert first.ignored == 1
    row = row_for(db, "101")
    assert row["state"] == "ignored"
    assert row["ignored_by"] == "auto"

    settings_store.set_values(db, {"min_score": "0"})
    second = svc.check_subscription(sub_id, manual=True)
    assert second.candidates == 1
    row = row_for(db, "101")
    assert row["state"] == "candidate"
    assert row["ignored_by"] is None


def test_manual_ignore_is_never_revived(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(result(torrent()), result(torrent()))
    svc = service(settings, db, mt)
    svc.check_subscription(sub_id, manual=True)
    with db.read() as conn:
        torrent_id = conn.execute("SELECT id FROM torrent WHERE mt_id = '101'").fetchone()["id"]
    Matcher(db).mark_manual_ignore(int(torrent_id))
    summary = svc.check_subscription(sub_id, manual=True)
    assert summary.candidates == 0
    row = row_for(db, "101")
    assert row["state"] == "ignored"
    assert row["ignored_by"] == "manual"


def test_error_marks_subscription(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(MTeamError("boom"))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error == "boom"
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["error_count"] == 1
    assert sub["last_error"] == "boom"


def test_rate_limit_does_not_count_as_failure(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(RateLimitBusy(20))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is not None
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["error_count"] == 0
    assert sub["last_error"] is None


def test_mode_falls_back_to_normal(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(MTeamApiError("400", "bad mode"), result(torrent()))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert [call[1]["mode"] for call in mt.calls] == ["tvshow", "normal"]


def test_empty_result_falls_back_to_original_title(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="中文剧名", year=2025)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET original_title = 'English Title' WHERE id = ?", (sub_id,)
        )
    mt = FakeMT(result(), result(torrent(name="English.Title.S01E02.1080p-G")))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert [call[0] for call in mt.calls] == ["中文剧名", "English Title"]
    assert summary.candidates == 1


def test_title_mismatch_not_stored(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    mt = FakeMT(result(torrent(name="毫无关系的剧.S01E01.1080p-G")))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.candidates == 0
    assert summary.ignored == 0
    with db.read() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM torrent").fetchone()["n"]
    assert count == 0


def test_auto_grab_when_enabled(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    downloader = FakeDownloader()
    summary = service(settings, db, FakeMT(result(torrent())), downloader).check_subscription(
        sub_id, manual=True
    )
    assert summary.grabbed == 1
    assert downloader.called == [
        int(row_for(db, "101")["id"])
    ]


def test_auto_grab_skipped_when_dry_run(ctx, tmp_path: Path) -> None:
    settings, db = ctx
    settings.dry_run = True
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    downloader = FakeDownloader()
    summary = service(settings, db, FakeMT(result(torrent())), downloader).check_subscription(
        sub_id, manual=True
    )
    assert summary.grabbed == 0
    assert downloader.called == []
    assert summary.grab_note is not None
    assert "DRY_RUN" in summary.grab_note


def test_auto_grab_note_when_auto_grab_is_off(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    summary = service(
        settings, db, FakeMT(result(torrent())), FakeDownloader()
    ).check_subscription(sub_id, manual=True)
    assert summary.grabbed == 0
    assert summary.grab_note == "未开启自动下载"


def test_auto_grab_note_when_every_aired_episode_is_covered(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    set_episode_info(db, sub_id, total=3, available=2)
    candidate = insert_torrent(
        db, sub_id, mt_id="e2", title="示例剧.S01E02.1080p-G", episode_start=2
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (candidate,))
        for episode in (1, 2):
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
                "VALUES (?, 1, ?, 'completed', ?)",
                (sub_id, episode, utc_now_iso()),
            )
    downloader = FakeDownloader()
    summary = service(settings, db, FakeMT(), downloader).check_subscription(
        sub_id, manual=True
    )
    assert summary.grabbed == 0
    assert downloader.called == []
    assert summary.grab_note == "已播出剧集均已下载或在下载中"


def test_auto_grab_note_reports_the_last_grab_error(ctx) -> None:
    class FailingDownloader:
        def grab(self, torrent_id: int, *, actor: str) -> GrabResult:
            raise MTeamError("qB 连接失败")

    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    candidate = insert_torrent(db, sub_id, mt_id="e1", title="示例剧.S01E01.1080p-G")
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (candidate,))
    summary = service(
        settings, db, FakeMT(), FailingDownloader()  # type: ignore[arg-type]
    ).check_subscription(sub_id, manual=True)
    assert summary.grabbed == 0
    assert summary.grab_note == "qB 连接失败"


def test_paused_subscription_skipped_automatically(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET status = 'paused' WHERE id = ?", (sub_id,))
    mt = FakeMT(result(torrent()))
    summary = service(settings, db, mt).check_subscription(sub_id)
    assert summary.error is not None
    assert mt.calls == []


def test_existing_failed_row_is_not_revived(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    insert_torrent(db, sub_id, mt_id="101", state="failed")
    summary = service(settings, db, FakeMT(result(torrent()))).check_subscription(
        sub_id, manual=True
    )
    assert summary.candidates == 0
    assert row_for(db, "101")["state"] == "failed"


def test_auto_grab_prefers_batch_covering_missing_episodes(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(
        db, title="示例剧", year=2025, episode_from=1, episode_to=10
    )
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    mt = FakeMT(
        result(
            torrent(mt_id="single", name="示例剧.S01E10.1080p.WEB-DL-GROUP"),
            torrent(mt_id="batch", name="示例剧.全10集.1080p.WEB-DL-GROUP"),
        )
    )
    downloader = FakeDownloader()
    summary = service(settings, db, mt, downloader).check_subscription(sub_id, manual=True)
    assert summary.grabbed == 1
    assert downloader.called == [int(row_for(db, "batch")["id"])]


def test_grab_best_picks_highest_score_without_explicit_range(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    low = insert_torrent(
        db, sub_id, mt_id="low", title="示例剧.S01E01.720p-G", state="candidate"
    )
    high = insert_torrent(
        db, sub_id, mt_id="high", title="示例剧.S01E01.1080p-G", state="candidate"
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 50 WHERE id = ?", (low,))
        conn.execute("UPDATE torrent SET score = 90 WHERE id = ?", (high,))
    downloader = FakeDownloader()
    result = service(settings, db, FakeMT(), downloader).grab_best(sub_id)
    assert result is not None
    assert result.torrent_id == high
    assert downloader.called == [high]


def test_pt_total_allows_backfill_outside_weekly_window(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    old = to_iso(utc_now() - timedelta(days=60))
    mt = FakeMT(
        result(
            torrent(
                name="示例剧.S01E02.1080p.WEB-DL-GROUP",
                description="示例剧 | 全10集 | 主演",
                created_at=old,
            )
        )
    )
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert summary.candidates == 1
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["total_episodes"] == 10
    assert sub["available_episode"] == 2
    assert sub["episodes_source"] == "PT"


def test_douban_total_wins_when_pt_description_undercounts(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '34937650' WHERE id = ?", (sub_id,)
        )
    douban = FakeDouban(total=12)
    mt = FakeMT(
        result(
            torrent(
                name="示例剧.S01E02.1080p.WEB-DL-GROUP",
                description="示例剧 | 全10集",
            )
        )
    )
    service(settings, db, mt, douban=douban).check_subscription(sub_id, manual=True)
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["total_episodes"] == 12
    assert sub["episodes_source"] == "豆瓣"


def test_douban_fills_total_when_pt_description_lacks_it(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '34937650' WHERE id = ?", (sub_id,)
        )
    douban = FakeDouban(total=12, available=5)
    mt = FakeMT(
        result(
            torrent(
                name="示例剧.S01E02.1080p.WEB-DL-GROUP",
                description="示例剧 | 第1季第2集",
            )
        )
    )
    summary = service(settings, db, mt, douban=douban).check_subscription(
        sub_id, manual=True
    )
    assert summary.error is None
    assert summary.candidates == 1
    assert douban.calls == ["34937650"]
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["total_episodes"] == 12
    assert sub["available_episode"] == 5
    assert sub["episodes_source"] == "豆瓣"


def test_douban_season_subject_is_corrected_and_matched(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="老友记 第二季", year=None, season=1)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '3286528' WHERE id = ?", (sub_id,)
        )
    douban = FakeDouban(
        total=24,
        original_title="Friends Season 2",
        aliases=("六人行 第二季",),
        year=1995,
    )
    mt = FakeMT(
        result(
            torrent(
                mt_id="friends",
                name="Friends.S02.2160p.WEB-DL.H265-GROUP",
                description="老友记 / 六人行 第二季 | 全24集 | 主演",
            )
        )
    )
    summary = service(settings, db, mt, douban=douban).check_subscription(
        sub_id, manual=True
    )
    assert summary.error is None
    assert summary.candidates == 1
    assert summary.searched == 1
    # Only one Douban call: shared between metadata preparation and episodes.
    assert douban.calls == ["3286528"]
    # The base Chinese title is searched first, then the English one.
    assert mt.calls[0][0] == "老友记"
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["season"] == 2
    assert sub["original_title"] == "Friends Season 2"
    assert sub["year"] == 1995
    assert json.loads(sub["aliases"]) == ["六人行 第二季"]
    assert sub["total_episodes"] == 24
    row = row_for(db, "friends")
    assert row["episode_start"] == 1
    assert row["episode_end"] == 24
    assert row["is_batch"] == 1


def test_season_correction_keeps_existing_progress(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="老友记 第二季", year=1995, season=1)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET douban_id = '3286528' WHERE id = ?", (sub_id,)
        )
        conn.execute(
            "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
            "VALUES (?, 1, 1, 'organized', ?)",
            (sub_id, utc_now_iso()),
        )
    douban = FakeDouban(total=24, original_title="Friends Season 2")
    service(settings, db, FakeMT(), douban=douban).check_subscription(sub_id, manual=True)
    with db.read() as conn:
        sub = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    assert sub["season"] == 1


def test_oversized_4k_pack_does_not_stop_smaller_pack_search(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="老友记 第二季", year=1995, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET original_title = 'Friends Season 2' WHERE id = ?",
            (sub_id,),
        )
    mt = FakeMT(
        result(
            torrent(
                mt_id="uhd",
                name="Friends.S02.2160p.WEB-DL-G",
                size_bytes=80 * 1024**3,
                discount="",
            )
        ),
        result(
            torrent(
                mt_id="hd",
                name="Friends.S02.1080p.WEB-DL-G",
                size_bytes=20 * 1024**3,
            )
        ),
    )
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert [call[0] for call in mt.calls] == ["老友记", "老友记 第二季"]
    assert row_for(db, "hd")["state"] == "candidate"
    assert row_for(db, "uhd")["state"] == "ignored"
    reasons = json.loads(row_for(db, "uhd")["reasons"])
    assert any(item["rule"] == "预算" and not item["passed"] for item in reasons)


def test_rate_limited_second_keyword_keeps_first_results(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="老友记 第二季", year=1995, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET original_title = 'Friends Season 2' WHERE id = ?",
            (sub_id,),
        )
    mt = FakeMT(result(), RateLimitBusy(20))
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert summary.searched == 1


def test_english_base_title_is_searched_when_chinese_has_no_hits(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="老友记 第二季", year=1995, season=2)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET original_title = 'Friends Season 2' WHERE id = ?",
            (sub_id,),
        )
    mt = FakeMT(
        result(),
        result(),
        result(torrent(name="Friends.S02.2160p.WEB-DL-G")),
    )
    summary = service(settings, db, mt).check_subscription(sub_id, manual=True)
    assert summary.error is None
    assert [call[0] for call in mt.calls] == ["老友记", "老友记 第二季", "Friends"]
    assert summary.candidates == 1


def set_episode_info(db, sub_id: int, *, total: int | None, available: int | None) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = ?, available_episode = ? WHERE id = ?",
            (total, available, sub_id),
        )


def test_auto_grab_downloads_every_aired_episode(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="嫁金钗", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    set_episode_info(db, sub_id, total=6, available=3)
    mt = FakeMT(
        result(
            torrent(mt_id="e1", name="嫁金钗.S01E01.1080p.WEB-DL-G"),
            torrent(mt_id="e2", name="嫁金钗.S01E02.1080p.WEB-DL-G"),
            torrent(mt_id="e3", name="嫁金钗.S01E03.1080p.WEB-DL-G"),
        )
    )
    downloader = FakeDownloader()
    summary = service(settings, db, mt, downloader).check_subscription(
        sub_id, manual=True
    )

    assert summary.error is None
    assert summary.grabbed == 3
    expected = {int(row_for(db, mt_id)["id"]) for mt_id in ("e1", "e2", "e3")}
    assert set(downloader.called) == expected
    assert len(downloader.called) == 3


def test_auto_grab_takes_one_cumulative_pack_for_all_aired(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="嫁金钗", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    set_episode_info(db, sub_id, total=36, available=14)
    mt = FakeMT(
        result(
            torrent(mt_id="pack", name="嫁金钗.S01.更新至14集.1080p.WEB-DL-G"),
            torrent(mt_id="single", name="嫁金钗.S01E14.2160p.WEB-DL-G"),
        )
    )
    downloader = FakeDownloader()
    summary = service(settings, db, mt, downloader).check_subscription(
        sub_id, manual=True
    )

    assert summary.error is None
    assert summary.grabbed == 1
    assert downloader.called == [int(row_for(db, "pack")["id"])]


def test_grab_best_covers_all_aired_episodes(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=3, available=3)
    ids = []
    for episode in (1, 2, 3):
        torrent_id = insert_torrent(
            db,
            sub_id,
            mt_id=f"e{episode}",
            title=f"示例剧.S01E{episode:02d}.1080p-G",
            episode_start=episode,
        )
        with db.tx() as conn:
            conn.execute("UPDATE torrent SET score = 70 WHERE id = ?", (torrent_id,))
        ids.append(torrent_id)

    downloader = FakeDownloader()
    result = service(settings, db, FakeMT(), downloader).grab_best(sub_id)

    assert result is not None
    assert result.outcome == "grabbed"
    assert "3 个种子" in result.message
    assert set(downloader.called) == set(ids)


def test_grab_best_reports_when_all_aired_episodes_are_covered(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="示例剧", year=2025)
    set_episode_info(db, sub_id, total=2, available=2)
    candidate = insert_torrent(
        db, sub_id, mt_id="e2", title="示例剧.S01E02.1080p-G", episode_start=2
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (candidate,))
        for episode in (1, 2):
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
                "VALUES (?, 1, ?, 'completed', ?)",
                (sub_id, episode, utc_now_iso()),
            )
    downloader = FakeDownloader()
    result = service(settings, db, FakeMT(), downloader).grab_best(sub_id)

    assert result is not None
    assert result.outcome == "skipped"
    assert "均已下载" in result.message
    assert downloader.called == []


def test_airing_show_backfills_all_aired_episodes_without_known_total(ctx) -> None:
    settings, db = ctx
    sub_id = insert_subscription(db, title="嫁金钗", year=2025)
    with db.tx() as conn:
        conn.execute("UPDATE subscription SET auto_grab = 1 WHERE id = ?", (sub_id,))
    old = to_iso(utc_now() - timedelta(days=30))
    mt = FakeMT(
        result(
            torrent(mt_id="e1", name="嫁金钗.S01E01.1080p.WEB-DL-G", created_at=old),
            torrent(mt_id="e2", name="嫁金钗.S01E02.1080p.WEB-DL-G", created_at=old),
            torrent(mt_id="e3", name="嫁金钗.S01E03.1080p.WEB-DL-G", created_at=old),
        )
    )
    downloader = FakeDownloader()
    summary = service(settings, db, mt, downloader).check_subscription(
        sub_id, manual=True
    )

    assert summary.error is None
    assert summary.candidates == 3
    assert summary.grabbed == 3
    assert len(downloader.called) == 3
