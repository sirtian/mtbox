from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.douban import DoubanChart, DoubanItem
from app.clients.mteam import MTeamSearchResult, MTeamTorrent
from app.config import Settings
from app.errors import Conflict, DoubanError, RateLimitBusy
from app.main import create_app
from app.services.downloader import GrabResult
from app.services.search_service import CheckSummary
from app.timeutil import utc_now_iso
from tests.helpers import insert_subscription, insert_torrent, make_db

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    downloads = tmp_path / "downloads"
    media = tmp_path / "media"
    config = tmp_path / "config"
    for directory in (downloads, media, config):
        directory.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "mt_api_key": "test-key",
        "download_dir": downloads,
        "library_dir": media,
        "data_dir": config,
        "path_map_json": json.dumps([["/downloads", str(downloads)]]),
        "qb_username": "",
        "session_secret": "test-secret",
        "admin_username": "admin",
        "admin_password": "secret",
        "dry_run": True,
        "scheduler_enabled": False,
        "min_video_size_mb": 0,
        "organize_delay_sec": 0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(make_settings(tmp_path), run_remote_checks=False)
    with TestClient(app) as test_client:
        yield test_client


def csrf(client: TestClient, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = CSRF_RE.search(response.text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def login(client: TestClient, password: str = "secret"):
    token = csrf(client)
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": password,
            "csrf_token": token,
            "next": "/",
        },
    )
    assert response.status_code == 200
    return response


def _item(
    mt_id: str = "101",
    name: str = "示例剧.S01E02.1080p.WEB-DL.H.264-GROUP",
    title: str = "示例剧 | 2026 | 第1季全12集 | 主演",
) -> MTeamTorrent:
    return MTeamTorrent(
        id=mt_id,
        name=name,
        description="",
        title=title,
        category="tv",
        size_bytes=2 * 1024**3,
        file_count=1,
        created_at="2026-10-20 12:00:00",
        labels=("中字",),
        imdb=None,
        imdb_rating=None,
        douban="1234567",
        douban_rating=8.1,
        seeders=12,
        leechers=1,
        completed=40,
        discount="FREE",
        discount_end_at=None,
        visible=True,
        banned=False,
    )


class FakeMT:
    def __init__(self, items: list[MTeamTorrent], total_pages: int = 0) -> None:
        self.items = items
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.total_pages = total_pages

    def search(self, keyword: str, **kwargs: object) -> MTeamSearchResult:
        self.calls.append((keyword, kwargs))
        return MTeamSearchResult(
            keyword,
            tuple(self.items),
            len(self.items),
            1,
            50,
            self.total_pages,
        )


# ---------- auth ----------


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")

    tasks = client.get("/tasks", follow_redirects=False)
    assert tasks.status_code == 303


def test_docs_endpoints_disabled(client: TestClient) -> None:
    # There is no public JSON API; the generated docs must stay closed.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unknown_page_returns_404_status(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "页面不存在" in response.text


def test_login_page_has_csrf_and_no_credentials(client: TestClient) -> None:
    token = csrf(client)
    assert token
    response = client.get("/login")
    assert "密码" in response.text


def test_login_rejects_missing_csrf(client: TestClient) -> None:
    response = client.post(
        "/login", data={"username": "admin", "password": "secret", "csrf_token": ""}
    )
    assert response.status_code == 400
    assert "CSRF" in response.text


def test_login_wrong_password_shows_error(client: TestClient) -> None:
    response = login(client, password="wrong")
    assert "用户名或密码错误" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_success_and_dashboard(client: TestClient) -> None:
    login(client)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "启动自检" in dashboard.text
    assert "M1" in dashboard.text
    assert "仪表盘" in dashboard.text


def test_login_locks_after_repeated_failures(client: TestClient) -> None:
    token = csrf(client)
    for _ in range(5):
        client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong",
                "csrf_token": token,
                "next": "/",
            },
        )
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "secret",
            "csrf_token": token,
            "next": "/",
        },
    )
    assert response.status_code == 200
    assert "失败次数过多" in response.text


def test_logout_clears_session(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/tasks")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 303


# ---------- search ----------


def test_search_requires_login(client: TestClient) -> None:
    response = client.get("/search?keyword=x", follow_redirects=False)
    assert response.status_code == 303


def test_search_renders_results(client: TestClient) -> None:
    login(client)
    client.app.state.mt = FakeMT([_item()])  # type: ignore[attr-defined]
    response = client.get("/search?keyword=示例剧")
    assert response.status_code == 200
    assert "<th>标题</th>" in response.text
    assert "示例剧 | 2026 | 第1季全12集 | 主演" in response.text
    assert "示例剧.S01E02" in response.text
    assert "FREE" in response.text
    assert "预填订阅" in response.text
    assert client.app.state.mt.calls[0][0] == "示例剧"  # type: ignore[attr-defined]


def test_search_passes_group_sort_discount_and_pagination(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()], total_pages=5)
    client.app.state.mt = fake  # type: ignore[attr-defined]

    response = client.get(
        "/search?keyword=示例&category=movie&sort=seeders_desc"
        "&discount=FREE&size=20&page=2"
    )

    assert response.status_code == 200
    _, kwargs = fake.calls[0]
    assert kwargs["mode"] == "movie"
    assert kwargs["categories"] == ["401", "419", "420", "421", "439"]
    assert kwargs["sort_field"] == "SEEDERS"
    assert kwargs["sort_direction"] == "DESC"
    assert kwargs["discount"] == "FREE"
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert "排序：做种最多" in response.text
    assert "分类：电影" in response.text
    assert "page=3" in response.text


def test_search_subcategory_shortcut_and_leaf_category(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()])
    client.app.state.mt = fake  # type: ignore[attr-defined]

    client.get("/search?category=tvshow&sub=402")
    assert fake.calls[-1][1]["categories"] == ["402"]
    assert fake.calls[-1][1]["mode"] == "tvshow"

    client.get("/search?category=419")
    assert fake.calls[-1][1]["categories"] == ["419"]
    assert fake.calls[-1][1]["mode"] == "movie"


def test_search_adult_group_uses_adult_mode(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()])
    client.app.state.mt = fake  # type: ignore[attr-defined]

    client.get("/search?category=adult")

    assert fake.calls[-1][1]["mode"] == "adult"
    assert len(fake.calls[-1][1]["categories"]) == 15


def test_search_invalid_filters_fall_back(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()])
    client.app.state.mt = fake  # type: ignore[attr-defined]

    response = client.get("/search?keyword=x&category=bogus&sort=bogus&size=999")

    assert response.status_code == 200
    _, kwargs = fake.calls[0]
    assert kwargs["categories"] is None
    assert kwargs["sort_field"] == "CREATED_DATE"
    assert kwargs["sort_direction"] == "DESC"
    assert kwargs["page_size"] == 50


def test_search_empty_query_does_not_spend_budget(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()])
    client.app.state.mt = fake  # type: ignore[attr-defined]

    response = client.get("/search")

    assert response.status_code == 200
    assert fake.calls == []
    assert "输入关键词" in response.text


def test_search_browse_without_keyword(client: TestClient) -> None:
    login(client)
    fake = FakeMT([_item()])
    client.app.state.mt = fake  # type: ignore[attr-defined]

    response = client.get("/search?browse=1&category=anime&sort=size_desc")

    assert response.status_code == 200
    assert fake.calls[0][1]["categories"] == ["405", "453"]
    assert fake.calls[0][1]["mode"] == "anime"
    assert fake.calls[0][1]["sort_field"] == "SIZE"


def test_search_grab_preserves_search_query(client: TestClient) -> None:
    login(client)
    sub_id = insert_subscription(client.app.state.db, title="示例电影")  # type: ignore[attr-defined]
    token = csrf(client, "/search?keyword=x")
    response = client.post(
        "/search/grab",
        data={
            "csrf_token": token,
            "back": "/search?keyword=x&category=movie&sort=seeders_desc",
            "mt_id": "9999",
            "name": "Some.Movie.2026.1080p",
            "size_bytes": "1000",
            "seeders": "1",
            "discount": "",
            "category": "419",
            "publish_at": "2026-10-01 12:00:00",
            "subscription_id": str(sub_id),
            "keyword": "x",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/search?keyword=x&category=movie&sort=seeders_desc"
    )


# ---------- douban ----------


DOUBAN_COVER = (
    "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p2936709341.jpg"
)


class FakeDouban:
    def __init__(
        self,
        title: str = "榜单示例剧",
        total: int = 40,
        cover_url: str | None = DOUBAN_COVER,
    ) -> None:
        self.calls: list[tuple[str, str, int, int, bool, str, str]] = []
        self.title = title
        self.total = total
        self.cover_url = cover_url

    def fetch(
        self,
        kind: str = "tv",
        sort: str = "U",
        *,
        start: int = 0,
        count: int = 20,
        year: str = "",
        genre: str = "",
        refresh: bool = False,
    ) -> DoubanChart:
        self.calls.append((kind, sort, start, count, refresh, year, genre))
        return DoubanChart(
            kind=kind,
            sort=sort,
            items=(
                DoubanItem(
                    id="36675942",
                    kind="tv" if kind == "variety" else kind,
                    title=self.title,
                    year=2026,
                    rating=8.2,
                    rating_count=1234,
                    subtitle="2026 / 中国大陆 / 剧情",
                    episodes_info="更新至 12 集",
                    cover_url=self.cover_url,
                    url="https://movie.douban.com/subject/36675942/",
                    rank=start + 1,
                ),
            ),
            total=self.total,
            fetched_at=utc_now_iso(),
            has_more=start + count < self.total,
        )


def test_douban_requires_login(client: TestClient) -> None:
    response = client.get("/douban", follow_redirects=False)
    assert response.status_code == 303


def test_douban_page_renders_and_prefills(client: TestClient) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=tv&sort=U")
    assert response.status_code == 200
    assert "榜单示例剧" in response.text
    assert "更新至 12 集" in response.text
    assert "douban_id=36675942" in response.text
    assert "/douban/cover?url=" in response.text
    assert "img2.doubanio.com%2Fview%2Fphoto" in response.text
    assert 'src="https://img2.doubanio.com' not in response.text
    assert fake.calls == [("tv", "U", 0, 20, False, "", "")]

    refreshed = client.get("/douban?kind=movie&sort=R&refresh=1")
    assert refreshed.status_code == 200
    assert fake.calls[-1] == ("movie", "R", 0, 20, True, "", "")


def test_douban_page_paginates(client: TestClient) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    first = client.get("/douban?kind=tv&sort=U")
    assert first.status_code == 200
    assert "第 1 / 2 页" in first.text
    assert "start=20" in first.text
    assert "下一页" in first.text

    second = client.get("/douban?kind=tv&sort=U&start=20")
    assert second.status_code == 200
    assert fake.calls[-1] == ("tv", "U", 20, 20, False, "", "")
    assert "第 2 / 2 页" in second.text
    assert "start=0" in second.text
    assert "上一页" in second.text

    clamped = client.get("/douban?kind=tv&start=abc")
    assert clamped.status_code == 200
    assert fake.calls[-1] == ("tv", "U", 0, 20, False, "", "")


def test_douban_page_size_and_page_jump(client: TestClient) -> None:
    login(client)
    fake = FakeDouban(total=500)
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=movie&sort=S&size=50&year=2024&genre=喜剧")
    assert response.status_code == 200
    assert fake.calls[-1] == ("movie", "S", 0, 50, False, "2024", "喜剧")
    assert 'value="50" selected' in response.text
    assert "共约 500 条" in response.text
    assert "第 1 / 10 页" in response.text
    assert "size=50" in response.text
    assert "max=\"10\"" in response.text

    jumped = client.get(
        "/douban?kind=movie&sort=S&size=100&page=3&year=2024&genre=喜剧"
    )
    assert jumped.status_code == 200
    assert fake.calls[-1] == ("movie", "S", 200, 100, False, "2024", "喜剧")
    assert "第 3 / 5 页" in jumped.text
    assert 'value="100" selected' in jumped.text
    assert "跳到第" in jumped.text

    fallback = client.get("/douban?kind=tv&size=999&page=2")
    assert fallback.status_code == 200
    assert fake.calls[-1] == ("tv", "U", 20, 20, False, "", "")


def test_douban_page_prefills_the_subject_season(client: TestClient) -> None:
    login(client)
    fake = FakeDouban(title="老友记 第二季")
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=tv&sort=U")
    assert response.status_code == 200
    assert "season=2" in response.text


def test_search_prefills_season_for_season_pack(client: TestClient) -> None:
    login(client)
    client.app.state.mt = FakeMT(  # type: ignore[attr-defined]
        [_item(name="示例剧.S02.2160p.WEB-DL-G")]
    )
    response = client.get("/search?keyword=示例剧")
    assert response.status_code == 200
    assert "season=2" in response.text
    assert "kind=tv" in response.text


def test_search_prefill_carries_category_and_selected_torrent(
    client: TestClient,
) -> None:
    login(client)
    client.app.state.mt = FakeMT([_item()])  # type: ignore[attr-defined]

    response = client.get("/search?keyword=示例剧&category=anime")

    # “动画”映射到媒体库常用的“动漫”，并带上当前选中的种子。
    assert "library_category=%E5%8A%A8%E6%BC%AB" in response.text
    assert "mt_id=101" in response.text
    assert "mt_name=%E7%A4%BA%E4%BE%8B%E5%89%A7" in response.text
    assert "mt_size_bytes=2147483648" in response.text


def test_search_prefill_without_category_omits_library_category(
    client: TestClient,
) -> None:
    login(client)
    client.app.state.mt = FakeMT([_item()])  # type: ignore[attr-defined]

    response = client.get("/search?keyword=示例剧")

    assert "library_category=" not in response.text
    assert "mt_id=101" in response.text


def test_search_prefill_tvshow_maps_to_tv_library_category(
    client: TestClient,
) -> None:
    login(client)
    client.app.state.mt = FakeMT([_item()])  # type: ignore[attr-defined]

    response = client.get("/search?category=tvshow")

    assert "library_category=%E7%94%B5%E8%A7%86%E5%89%A7" in response.text


def test_search_prefill_uses_semantic_subcategory(client: TestClient) -> None:
    login(client)
    client.app.state.mt = FakeMT([_item()])  # type: ignore[attr-defined]

    semantic = client.get("/search?category=music&sub=406")
    assert "library_category=%E6%BC%94%E5%94%B1%E4%BC%9A" in semantic.text  # 演唱会

    # 画质档不覆盖父分类：电影/HD 仍然预填“电影”。
    quality = client.get("/search?category=movie&sub=419")
    assert "library_category=%E7%94%B5%E5%BD%B1" in quality.text


def test_douban_page_has_variety_tab(client: TestClient) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=variety&sort=S")
    assert response.status_code == 200
    assert "综艺" in response.text
    assert "kind=variety" in response.text
    assert "kind=tv" in response.text
    assert "season=1" in response.text
    assert fake.calls[-1] == ("variety", "S", 0, 20, False, "", "")


def test_douban_page_filters_by_year_and_genre(client: TestClient) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=movie&sort=S&year=2024&genre=喜剧")
    assert response.status_code == 200
    assert fake.calls[-1] == ("movie", "S", 0, 20, False, "2024", "喜剧")
    assert 'value="2024" selected' in response.text
    assert 'value="喜剧" selected' in response.text
    assert "筛选：2024 · 喜剧" in response.text
    # Tabs, sorting and the pager keep the active filters.
    assert "year=2024" in response.text
    assert "genre=%E5%96%9C%E5%89%A7" in response.text
    assert "清除筛选" in response.text


def test_douban_page_remembers_filters_after_subscribe_navigation(
    client: TestClient,
) -> None:
    login(client)
    fake = FakeDouban(total=500)
    client.app.state.douban = fake  # type: ignore[attr-defined]

    # 首次进入且没有筛选参数时使用默认条件
    client.get("/douban")
    assert fake.calls[-1] == ("tv", "U", 0, 20, False, "", "")

    client.get("/douban?kind=movie&sort=S&size=50&year=2024&genre=喜剧")
    assert fake.calls[-1] == ("movie", "S", 0, 50, False, "2024", "喜剧")

    # 点击“订阅”跳转到新建订阅页，再从导航回到榜单：沿用上次筛选
    client.get("/subscriptions/new?kind=movie&title=榜单示例剧&douban_id=36675942")
    restored = client.get("/douban")
    assert restored.status_code == 200
    assert fake.calls[-1] == ("movie", "S", 0, 50, False, "2024", "喜剧")
    assert 'value="2024" selected' in restored.text
    assert 'value="喜剧" selected' in restored.text
    assert 'value="50" selected' in restored.text

    # “清除筛选”后，再回到榜单不再带旧的年份/类别
    cleared = client.get("/douban?kind=movie&sort=S&size=50")
    assert cleared.status_code == 200
    assert fake.calls[-1] == ("movie", "S", 0, 50, False, "", "")
    again = client.get("/douban")
    assert fake.calls[-1] == ("movie", "S", 0, 50, False, "", "")


def test_douban_page_prefills_library_category_from_genre(
    client: TestClient,
) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]

    response = client.get("/douban?kind=movie&sort=S&year=2024&genre=喜剧")
    assert "library_category=%E5%96%9C%E5%89%A7" in response.text

    # 豆瓣的“动画”对应媒体库常用的“动漫”
    alias = client.get("/douban?kind=movie&sort=S&genre=动画")
    assert "library_category=%E5%8A%A8%E6%BC%AB" in alias.text

    # 未选类别时不预填分类
    plain = client.get("/douban?kind=tv&sort=U")
    assert "library_category=" not in plain.text


def test_subscription_new_prefills_library_category(client: TestClient) -> None:
    login(client)
    response = client.get(
        "/subscriptions/new?kind=movie&title=示例电影&library_category=动漫"
    )
    assert response.status_code == 200
    assert 'name="library_category" value="动漫"' in response.text


def test_subscription_new_keeps_selected_torrent(client: TestClient) -> None:
    login(client)
    response = client.get(
        "/subscriptions/new",
        params={
            "kind": "tv",
            "title": "示例剧",
            "library_category": "动漫",
            "mt_id": "101",
            "mt_name": "示例剧.S01E02.1080p.WEB-DL",
            "mt_size_bytes": "1000",
            "mt_seeders": "12",
            "mt_discount": "FREE",
            "mt_category": "402",
            "mt_publish_at": "2026-10-20 12:00:00",
        },
    )
    assert response.status_code == 200
    assert "保存后将直接下载所选资源" in response.text
    assert 'name="mt_id" value="101"' in response.text
    assert 'name="mt_name" value="示例剧.S01E02.1080p.WEB-DL"' in response.text
    assert 'name="mt_category" value="402"' in response.text
    assert 'name="mt_discount" value="FREE"' in response.text
    assert 'name="library_category" value="动漫"' in response.text


class RecordingDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def grab(self, torrent_id: int, *, actor: str = "manual") -> GrabResult:
        self.calls.append((torrent_id, actor))
        return GrabResult(torrent_id, "grabbed", "已开始下载")


def _prefilled_subscription_data(token: str) -> dict[str, str]:
    return {
        "kind": "tv",
        "title": "示例剧",
        "original_title": "",
        "year": "2026",
        "season": "1",
        "douban_id": "",
        "imdb_id": "",
        "library_category": "动漫",
        "episode_offset": "0",
        "episode_from": "",
        "episode_to": "",
        "interval_min": "30",
        "auto_grab": "",
        "mt_id": "101",
        "mt_name": "示例剧.S01E02.1080p.WEB-DL.H.264-GROUP",
        "mt_size_bytes": "2147483648",
        "mt_seeders": "12",
        "mt_discount": "FREE",
        "mt_category": "402",
        "mt_publish_at": "2026-10-20 12:00:00",
        "csrf_token": token,
    }


def test_create_subscription_from_search_result_grabs_immediately(
    client: TestClient,
) -> None:
    login(client)
    downloader = RecordingDownloader()
    client.app.state.downloader = downloader  # type: ignore[attr-defined]
    token = csrf(client, "/subscriptions/new")

    response = client.post(
        "/subscriptions/new",
        data=_prefilled_subscription_data(token),
    )

    assert response.status_code == 200
    assert "订阅「示例剧」已创建" in response.text
    assert "已开始下载" in response.text
    assert len(downloader.calls) == 1
    _, actor = downloader.calls[0]
    assert actor == "admin"

    with client.app.state.db.read() as conn:  # type: ignore[attr-defined]
        sub = conn.execute("SELECT * FROM subscription").fetchone()
        torrent = conn.execute("SELECT * FROM torrent").fetchone()
    assert sub["library_category"] == "动漫"
    assert torrent["subscription_id"] == sub["id"]
    assert torrent["mt_id"] == "101"
    assert torrent["discount"] == "FREE"
    assert torrent["category"] == "402"
    assert torrent["seeders"] == 12


def test_create_subscription_ignores_invalid_torrent_id(
    client: TestClient,
) -> None:
    login(client)
    downloader = RecordingDownloader()
    client.app.state.downloader = downloader  # type: ignore[attr-defined]
    token = csrf(client, "/subscriptions/new")
    data = _prefilled_subscription_data(token)
    data["mt_id"] = "not-a-number"

    response = client.post("/subscriptions/new", data=data)

    assert response.status_code == 200
    assert "订阅「示例剧」已创建" in response.text
    assert downloader.calls == []
    with client.app.state.db.read() as conn:  # type: ignore[attr-defined]
        assert conn.execute("SELECT COUNT(*) AS n FROM torrent").fetchone()["n"] == 0


def test_create_subscription_keeps_subscription_when_grab_fails(
    client: TestClient,
) -> None:
    login(client)

    class FailingDownloader:
        def grab(self, torrent_id: int, *, actor: str = "manual") -> GrabResult:
            raise Conflict("分享率过低，已暂停抓取")

    client.app.state.downloader = FailingDownloader()  # type: ignore[attr-defined]
    token = csrf(client, "/subscriptions/new")

    response = client.post(
        "/subscriptions/new",
        data=_prefilled_subscription_data(token),
    )

    assert response.status_code == 200
    assert "订阅「示例剧」已创建" in response.text
    assert "资源下载失败：分享率过低，已暂停抓取" in response.text
    with client.app.state.db.read() as conn:  # type: ignore[attr-defined]
        assert conn.execute("SELECT COUNT(*) AS n FROM subscription").fetchone()["n"] == 1


def test_douban_page_ignores_unknown_filters(client: TestClient) -> None:
    login(client)
    fake = FakeDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban?kind=tv&year=2999&genre=不存在")
    assert response.status_code == 200
    assert fake.calls[-1] == ("tv", "U", 0, 20, False, "", "")
    assert "全部年份" in response.text
    assert "全部类别" in response.text


def test_douban_page_surfaces_error(client: TestClient) -> None:
    login(client)

    class Failing:
        def fetch(self, *args: object, **kwargs: object) -> DoubanChart:
            raise DoubanError("豆瓣暂时不可用")

    client.app.state.douban = Failing()  # type: ignore[attr-defined]
    response = client.get("/douban")
    assert response.status_code == 200
    assert "豆瓣暂时不可用" in response.text


def test_douban_cover_requires_login(client: TestClient) -> None:
    response = client.get(
        "/douban/cover", params={"url": DOUBAN_COVER}, follow_redirects=False
    )
    assert response.status_code == 303


def test_douban_cover_proxies_image_with_cache_headers(client: TestClient) -> None:
    login(client)

    class CoverDouban:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch_cover(self, url: str) -> tuple[bytes, str]:
            self.calls.append(url)
            return b"\xff\xd8poster", "image/jpeg"

    fake = CoverDouban()
    client.app.state.douban = fake  # type: ignore[attr-defined]
    response = client.get("/douban/cover", params={"url": DOUBAN_COVER})
    assert response.status_code == 200
    assert response.content == b"\xff\xd8poster"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "max-age=86400" in response.headers["cache-control"]
    assert fake.calls == [DOUBAN_COVER]


def test_douban_cover_failure_returns_404(client: TestClient) -> None:
    login(client)

    class FailingCover:
        def fetch_cover(self, url: str) -> tuple[bytes, str]:
            raise DoubanError("bad cover")

    client.app.state.douban = FailingCover()  # type: ignore[attr-defined]
    response = client.get("/douban/cover", params={"url": DOUBAN_COVER})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


# ---------- subscriptions ----------


def test_create_subscription_and_list(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/subscriptions/new")
    response = client.post(
        "/subscriptions/new",
        data={
            "kind": "tv",
            "title": "示例剧",
            "original_title": "Example Show",
            "year": "2025",
            "season": "1",
            "douban_id": "",
            "imdb_id": "",
            "library_category": " 动漫 ",
            "episode_offset": "0",
            "episode_from": "",
            "episode_to": "",
            "interval_min": "30",
            "auto_grab": "",
            "csrf_token": token,
        },
    )
    assert response.status_code == 200
    assert "示例剧" in response.text

    with client.app.state.db.read() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT * FROM subscription").fetchone()
    assert row["title"] == "示例剧"
    assert row["status"] == "active"
    assert row["library_category"] == "动漫"
    assert "动漫" in response.text


def test_edit_subscription_library_category(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧", library_category="电视剧")

    page = client.get(f"/subscriptions/{sub_id}/edit")
    assert page.status_code == 200
    assert "媒体库分类" in page.text
    assert 'value="电视剧"' in page.text

    token = csrf(client, f"/subscriptions/{sub_id}/edit")
    response = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "title": "示例剧",
            "year": "2025",
            "season": "1",
            "library_category": " 动漫 ",
            "episode_offset": "0",
            "interval_min": "30",
            "csrf_token": token,
        },
    )
    assert response.status_code == 200
    assert "动漫" in response.text
    with db.read() as conn:
        row = conn.execute(
            "SELECT library_category FROM subscription WHERE id = ?", (sub_id,)
        ).fetchone()
    assert row["library_category"] == "动漫"

    token = csrf(client, f"/subscriptions/{sub_id}/edit")
    client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "title": "示例剧",
            "year": "2025",
            "season": "1",
            "library_category": "",
            "episode_offset": "0",
            "interval_min": "30",
            "csrf_token": token,
        },
    )
    with db.read() as conn:
        row = conn.execute(
            "SELECT library_category FROM subscription WHERE id = ?", (sub_id,)
        ).fetchone()
    assert row["library_category"] is None


def test_edit_subscription_requires_csrf(client: TestClient) -> None:
    login(client)
    sub_id = insert_subscription(client.app.state.db, title="示例剧")  # type: ignore[attr-defined]
    response = client.post(
        f"/subscriptions/{sub_id}/edit", data={"title": "改名"}
    )
    assert response.status_code == 400


def test_create_subscription_requires_csrf(client: TestClient) -> None:
    login(client)
    response = client.post("/subscriptions/new", data={"kind": "tv", "title": "x"})
    assert response.status_code == 400


def test_tv_subscription_shows_total_downloaded_and_remaining(
    client: TestClient,
) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 29, available_episode = 5, "
            "episodes_source = '豆瓣' WHERE id = ?",
            (sub_id,),
        )
        conn.executemany(
            "INSERT INTO episode_state(subscription_id, season, episode, state, updated_at) "
            "VALUES (?, 1, ?, ?, ?)",
            [
                (sub_id, 1, "organized", utc_now_iso()),
                (sub_id, 2, "organized", utc_now_iso()),
                (sub_id, 3, "grabbed", utc_now_iso()),
            ],
        )
    response = client.get("/subscriptions")
    assert response.status_code == 200
    assert "总 29 集" in response.text
    assert "已下载 3 集" in response.text
    assert "待下载 2 集" in response.text
    assert "待上线 24 集" in response.text
    assert "总集数来自 豆瓣" in response.text


def test_movie_subscription_progress_follows_task_state(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, kind="movie", title="飞驰人生3", year=2026)

    # 只有候选时不算已整理
    insert_torrent(db, sub_id, mt_id="m0", title="飞驰人生3.候选", state="candidate")
    page = client.get("/subscriptions")
    assert "未整理" in page.text

    # 任务整理完成后订阅页同步显示已整理
    insert_torrent(db, sub_id, mt_id="m1", title="飞驰人生3.2026.1080p", state="organized")
    page = client.get("/subscriptions")
    assert "已整理" in page.text
    assert "未整理" not in page.text

    # 后续新抓取的任务优先反映为下载中
    insert_torrent(db, sub_id, mt_id="m2", title="飞驰人生3.2026.2160p", state="downloading")
    page = client.get("/subscriptions")
    assert "下载中" in page.text
    assert "未整理" not in page.text

    # 下载完成、尚未整理
    insert_torrent(db, sub_id, mt_id="m3", title="飞驰人生3.2026.2160p.v2", state="completed")
    page = client.get("/subscriptions")
    assert "待整理" in page.text


def test_subscriptions_done_sheet_separates_finished(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    insert_subscription(db, title="进行中的剧")
    insert_subscription(db, title="暂停的剧", status="paused")

    empty = client.get("/subscriptions?view=done")
    assert empty.status_code == 200
    assert "还没有已完成的订阅" in empty.text

    insert_subscription(db, title="已完成的电影", kind="movie", status="done")

    active = client.get("/subscriptions")
    assert active.status_code == 200
    assert "进行中的剧" in active.text
    assert "暂停的剧" in active.text
    assert "已完成的电影" not in active.text
    assert "进行中 (2)" in active.text
    assert "已完成 (1)" in active.text

    done = client.get("/subscriptions?view=done")
    assert done.status_code == 200
    assert "已完成的电影" in done.text
    assert "进行中的剧" not in done.text
    assert "暂停的剧" not in done.text


def test_toggle_auto_grab_on_existing_subscription(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db)

    def auto_grab() -> int:
        with db.read() as conn:
            row = conn.execute(
                "SELECT auto_grab FROM subscription WHERE id = ?", (sub_id,)
            ).fetchone()
        return int(row["auto_grab"])

    assert auto_grab() == 0
    token = csrf(client, "/subscriptions")
    turned_on = client.post(
        f"/subscriptions/{sub_id}/auto-grab",
        data={"enabled": "1", "csrf_token": token},
    )
    assert turned_on.status_code == 200
    assert "已开启自动下载后续新集" in turned_on.text
    assert auto_grab() == 1

    turned_off = client.post(
        f"/subscriptions/{sub_id}/auto-grab",
        data={"enabled": "0", "csrf_token": token},
    )
    assert turned_off.status_code == 200
    assert "已关闭自动下载" in turned_off.text
    assert auto_grab() == 0


# ---------- grab ----------


def test_grab_creates_candidate_and_respects_dry_run(client: TestClient) -> None:
    login(client)
    sub_id = insert_subscription(client.app.state.db, title="示例剧")  # type: ignore[attr-defined]
    token = csrf(client, "/tasks")
    response = client.post(
        "/search/grab",
        data={
            "csrf_token": token,
            "keyword": "示例剧",
            "mt_id": "777",
            "name": "示例剧.S01E02.1080p.WEB-DL.H.264-GROUP",
            "size_bytes": "1000",
            "seeders": "5",
            "discount": "FREE",
            "category": "tv",
            "publish_at": "2026-10-20 12:00:00",
            "subscription_id": str(sub_id),
        },
    )
    assert response.status_code == 200
    assert "DRY_RUN" in response.text

    with client.app.state.db.read() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT * FROM torrent WHERE mt_id = '777'").fetchone()
    assert row is not None
    assert row["subscription_id"] == sub_id
    assert row["state"] == "candidate"
    assert row["season"] == 1
    assert row["episode_start"] == 2
    assert row["episode_end"] is None


def test_grab_rejects_foreign_torrent(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    first = insert_subscription(db, title="A")
    second = insert_subscription(db, title="B")
    insert_torrent(db, first, mt_id="888")
    token = csrf(client, "/tasks")
    response = client.post(
        "/search/grab",
        data={
            "csrf_token": token,
            "keyword": "",
            "mt_id": "888",
            "name": "示例剧.S01E01.1080p-G",
            "size_bytes": "1000",
            "seeders": "5",
            "discount": "",
            "category": "",
            "publish_at": "",
            "subscription_id": str(second),
        },
    )
    assert response.status_code == 200
    assert "其他订阅" in response.text


# ---------- tasks ----------


def test_tasks_page_and_poll(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    insert_torrent(
        db,
        sub_id,
        mt_id="901",
        title="示例剧.S01E03.1080p-GROUP",
        state="candidate",
        episode_start=3,
    )
    page = client.get("/tasks")
    assert page.status_code == 200
    # 候选默认不在任务列表中出现
    assert "示例剧.S01E03" not in page.text
    assert "显示候选与已忽略 (1)" in page.text

    all_page = client.get("/tasks?show=all")
    assert all_page.status_code == 200
    assert "示例剧.S01E03" in all_page.text
    assert "S01E03" in all_page.text
    assert "隐藏候选与已忽略" in all_page.text

    token = csrf(client, "/tasks")
    polled = client.post("/tasks/poll", data={"csrf_token": token})
    assert polled.status_code == 200
    assert "轮询完成" in polled.text


def test_tasks_page_hides_candidates_and_ignored_by_default(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="隐藏测试")
    insert_torrent(db, sub_id, mt_id="c2", title="隐藏测试.候选条目", state="candidate")
    insert_torrent(db, sub_id, mt_id="i2", title="隐藏测试.忽略条目", state="ignored")
    insert_torrent(db, sub_id, mt_id="d2", title="隐藏测试.下载中条目", state="downloading")

    page = client.get("/tasks")
    assert page.status_code == 200
    assert "隐藏测试.候选条目" not in page.text
    assert "隐藏测试.忽略条目" not in page.text
    assert "隐藏测试.下载中条目" in page.text
    assert "显示候选与已忽略 (2)" in page.text

    all_page = client.get("/tasks?show=all")
    assert "隐藏测试.候选条目" in all_page.text
    assert "隐藏测试.忽略条目" in all_page.text


def test_tasks_page_empty_state_mentions_hidden_candidates(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="仅候选")
    insert_torrent(db, sub_id, mt_id="c3", title="仅候选.条目", state="candidate")

    page = client.get("/tasks")
    assert page.status_code == 200
    assert "已隐藏 1 个候选/已忽略条目" in page.text
    assert "显示候选与已忽略 (1)" in page.text


def test_tasks_organized_tasks_have_their_own_view(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="归档测试")

    empty = client.get("/tasks?view=organized")
    assert empty.status_code == 200
    assert "还没有已整理的任务" in empty.text

    insert_torrent(db, sub_id, mt_id="d1", title="归档测试.下载中", state="downloading")
    insert_torrent(db, sub_id, mt_id="o1", title="归档测试.已整理", state="organized")

    active = client.get("/tasks")
    assert active.status_code == 200
    assert "归档测试.下载中" in active.text
    assert "归档测试.已整理" not in active.text
    assert "已整理 (1)" in active.text
    assert "下载与整理 (1)" in active.text

    organized = client.get("/tasks?view=organized")
    assert organized.status_code == 200
    assert "归档测试.已整理" in organized.text
    assert "归档测试.下载中" not in organized.text
    assert "重新整理" in organized.text
    # 候选切换与整理待处理只属于进行中任务页
    assert "显示候选与已忽略" not in organized.text
    assert "整理待处理" not in organized.text


def test_tasks_retry_only_for_failed(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    torrent_id = insert_torrent(db, sub_id, mt_id="902", state="failed")
    token = csrf(client, "/tasks")
    response = client.post(f"/tasks/{torrent_id}/retry", data={"csrf_token": token})
    assert response.status_code == 200
    with db.read() as conn:
        row = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    assert row["state"] == "candidate"


def test_task_delete_removes_failed_task_from_list(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="删除测试")
    torrent_id = insert_torrent(
        db, sub_id, mt_id="903", title="删除测试.失败任务", state="failed"
    )
    now = utc_now_iso()
    with db.tx() as conn:
        for episode, state in ((1, "downloading"), (2, "organized")):
            conn.execute(
                "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, "
                "state, updated_at) VALUES (?, 1, ?, ?, ?, ?)",
                (sub_id, episode, torrent_id, state, now),
            )

    assert "删除测试.失败任务" in client.get("/tasks").text

    token = csrf(client, "/tasks")
    response = client.post(f"/tasks/{torrent_id}/delete", data={"csrf_token": token})
    assert response.status_code == 200
    assert "任务已删除" in response.text

    with db.read() as conn:
        row = conn.execute(
            "SELECT state, deleted_at FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
        episodes = conn.execute(
            "SELECT episode, state FROM episode_state WHERE torrent_id = ? ORDER BY episode",
            (torrent_id,),
        ).fetchall()
    assert row["state"] == "deleted"
    assert row["deleted_at"]
    # 未整理的集数释放出来以便重新抓取，已整理的集数保持不变。
    assert [(item["episode"], item["state"]) for item in episodes] == [(2, "organized")]

    assert "删除测试.失败任务" not in client.get("/tasks").text
    assert "删除测试.失败任务" not in client.get("/tasks?show=all").text


def test_task_delete_hides_candidate_even_with_show_all(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="候选删除")
    torrent_id = insert_torrent(
        db, sub_id, mt_id="904", title="候选删除.条目", state="candidate"
    )
    assert "候选删除.条目" in client.get("/tasks?show=all").text

    token = csrf(client, "/tasks")
    response = client.post(f"/tasks/{torrent_id}/delete", data={"csrf_token": token})
    assert response.status_code == 200

    with db.read() as conn:
        row = conn.execute(
            "SELECT state, deleted_at FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
    assert row["state"] == "deleted"
    assert row["deleted_at"]
    assert "候选删除.条目" not in client.get("/tasks?show=all").text


def test_logs_page(client: TestClient) -> None:
    login(client)
    response = client.get("/logs")
    assert response.status_code == 200
    assert "事件日志" in response.text


# ---------- candidates ----------


class FakeSearchService:
    def __init__(self, summary: CheckSummary) -> None:
        self.summary = summary
        self.calls: list[tuple[int, bool]] = []

    def check_subscription(self, sub_id: int, *, manual: bool = False) -> CheckSummary:
        self.calls.append((sub_id, manual))
        return self.summary


def test_candidates_page_and_actions(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    torrent_id = insert_torrent(db, sub_id, mt_id="950", state="candidate")
    page = client.get("/candidates")
    assert page.status_code == 200
    assert "示例剧.S01E01" in page.text

    token = csrf(client, "/tasks")
    ignored = client.post(
        f"/candidates/{torrent_id}/ignore", data={"csrf_token": token}
    )
    assert ignored.status_code == 200
    with db.read() as conn:
        row = conn.execute(
            "SELECT state, ignored_by FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
    assert row["state"] == "ignored"
    assert row["ignored_by"] == "manual"

    revived = client.post(
        f"/candidates/{torrent_id}/revive", data={"csrf_token": token}
    )
    assert revived.status_code == 200
    with db.read() as conn:
        row = conn.execute(
            "SELECT state, ignored_by FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
    assert row["state"] == "candidate"
    assert row["ignored_by"] is None


def test_manual_subscription_check(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    fake = FakeSearchService(CheckSummary(sub_id, searched=1, candidates=2))
    client.app.state.search = fake  # type: ignore[attr-defined]
    token = csrf(client, "/subscriptions")
    response = client.post(
        f"/subscriptions/{sub_id}/check", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "检查完成" in response.text
    assert fake.calls == [(sub_id, True)]


def test_manual_check_flash_explains_zero_grab(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    fake = FakeSearchService(
        CheckSummary(
            sub_id,
            searched=1,
            candidates=2,
            grab_note="DRY_RUN 已开启，计划抓取 1 个种子（未实际下载）",
        )
    )
    client.app.state.search = fake  # type: ignore[attr-defined]
    token = csrf(client, "/subscriptions")
    response = client.post(
        f"/subscriptions/{sub_id}/check", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "抓取 0" in response.text
    assert "DRY_RUN" in response.text


# ---------- settings / M3 dashboard ----------


SETTINGS_FORM = {
    "default_interval_min": "30",
    "max_candidates": "20",
    "candidate_ttl_days": "7",
    "season_pack_min_missing": "3",
    "new_episode_window_days": "7",
    "min_seed_hours": "72",
    "max_seed_hours": "336",
    "daily_download_budget_gb": "50",
    "disk_pause_percent": "90",
    "min_score": "45",
    "min_ratio": "1.0",
    "ratio_pause_nonfree": "0.8",
    "ratio_pause_all": "0.6",
    "movie_path_template": "{library}/Movies/{title} ({year})/{title}.{ext}",
    "tv_path_template": "{library}/TV/{title}/S{season:02d}/{title} - S{season:02d}E{episode:02d}.{ext}",
    "mode_map": '{"movie": "normal", "tv": "tvshow"}',
    "category_map": "{}",
    "notify_config": "{}",
}


def test_dashboard_shows_guard_disk_and_jobs(client: TestClient) -> None:
    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "分享率护栏" in response.text
    assert "媒体库磁盘" in response.text
    assert "定时任务" in response.text
    assert "调度器未启用" in response.text


def test_dashboard_formats_account_sizes(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO account_snapshot(ratio, uploaded, downloaded, bonus, captured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1.5, 4 * 1024**4, 512 * 1024**3, 100.0, "2026-10-28T00:00:00Z"),
        )
    response = client.get("/")
    assert "4.00 TB" in response.text
    assert "512.00 GB" in response.text
    assert 'aria-current="page"' in response.text


def test_settings_requires_login(client: TestClient) -> None:
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_settings_save_updates_values(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"min_score": "60", "auto_grab_default": "on", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"

    from app.services import settings_store

    db = client.app.state.db
    assert settings_store.get_float(db, "min_score", 0) == 60.0
    assert settings_store.get_bool(db, "auto_grab_default", False) is True

    page = client.get("/settings")
    assert 'value="60.0"' in page.text


def test_settings_search_budget_updates_limiter_without_restart(client: TestClient) -> None:
    login(client)
    from app.services import settings_store

    db = client.app.state.db  # type: ignore[attr-defined]
    limiter = client.app.state.limiter  # type: ignore[attr-defined]
    env_budget = client.app.state.settings.mt_search_daily_budget  # type: ignore[attr-defined]

    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"search_daily_budget": "555", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert settings_store.effective_search_budget(db, env_budget) == 555
    assert limiter.budgets()["search"] == 555

    page = client.get("/settings")
    assert 'name="search_daily_budget"' in page.text
    assert 'value="555"' in page.text

    # Blank field removes the override and falls back to .env.
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload["csrf_token"] = token
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert settings_store.get_value(db, "search_daily_budget") is None
    assert limiter.budgets()["search"] == env_budget

    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"search_daily_budget": "0", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert "search_daily_budget 必须在 1..100000" in response.text
    assert settings_store.get_value(db, "search_daily_budget") is None


def test_settings_search_interval_updates_limiter_without_restart(
    client: TestClient,
) -> None:
    login(client)
    from app.services import settings_store

    db = client.app.state.db  # type: ignore[attr-defined]
    limiter = client.app.state.limiter  # type: ignore[attr-defined]
    env_interval = client.app.state.settings.mt_search_min_interval_sec  # type: ignore[attr-defined]

    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"search_min_interval_sec": "30", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert settings_store.effective_search_min_interval(db, env_interval) == 30
    limiter.acquire("search")
    with pytest.raises(RateLimitBusy):
        limiter.acquire("search", wait=False)

    page = client.get("/settings")
    assert 'name="search_min_interval_sec"' in page.text
    assert 'value="30"' in page.text

    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"search_min_interval_sec": "0", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert settings_store.effective_search_min_interval(db, env_interval) == 0
    limiter.acquire("search", wait=False)

    # Blank removes the override and falls back to the .env default.
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload["csrf_token"] = token
    client.post("/settings", data=payload, follow_redirects=False)
    assert settings_store.get_value(db, settings_store.SEARCH_MIN_INTERVAL_KEY) is None
    assert settings_store.effective_search_min_interval(db, env_interval) == env_interval

    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"search_min_interval_sec": "-1", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert "search_min_interval_sec 必须在 0..86400" in response.text
    assert settings_store.get_value(db, settings_store.SEARCH_MIN_INTERVAL_KEY) is None


def test_settings_rejects_invalid_json(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"min_score": "70", "notify_config": "{bad", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert "通知配置不是合法 JSON" in response.text

    from app.services import settings_store

    assert settings_store.get_float(client.app.state.db, "min_score", 0) == 45.0


def test_settings_rejects_bad_template(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"tv_path_template": "{library}/{unknown}.{ext}", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert "未知占位符" in response.text


def test_settings_rejects_cross_thresholds(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"ratio_pause_all": "0.9", "ratio_pause_nonfree": "0.8", "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert "ratio_pause_all 不能大于" in response.text


def test_settings_shows_credential_status_without_plaintext(client: TestClient) -> None:
    login(client)
    page = client.get("/settings")
    assert page.status_code == 200
    assert "已配置（环境变量）" in page.text
    assert "test-key" not in page.text
    assert 'name="mt_api_key"' in page.text
    assert 'name="qb_password"' in page.text
    assert 'type="password"' in page.text


def test_settings_saves_mt_key_masks_it_and_updates_client(client: TestClient) -> None:
    login(client)
    secret = "rotated-mt-key-9f3a"
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"mt_api_key": secret, "csrf_token": token})
    response = client.post("/settings", data=payload, follow_redirects=False)
    assert response.status_code == 303

    from app.services import settings_store

    db = client.app.state.db
    assert settings_store.get_value(db, "mt_api_key") == secret
    assert client.app.state.settings.mt_api_key == secret
    assert client.app.state.mt._client.headers["x-api-key"] == secret

    page = client.get("/settings")
    assert secret not in page.text
    assert "已配置（设置页）" in page.text

    with db.read() as conn:
        row = conn.execute(
            "SELECT message, payload FROM event_log WHERE source = 'settings' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert secret not in (row["message"] or "")
    assert secret not in (row["payload"] or "")


def test_settings_blank_secret_keeps_saved_value(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"mt_api_key": "keep-me-key", "csrf_token": token})
    client.post("/settings", data=payload, follow_redirects=False)

    token = csrf(client, "/settings")
    second = dict(SETTINGS_FORM)
    second.update({"csrf_token": token})
    client.post("/settings", data=second, follow_redirects=False)

    from app.services import settings_store

    db = client.app.state.db
    assert settings_store.get_value(db, "mt_api_key") == "keep-me-key"
    assert client.app.state.settings.mt_api_key == "keep-me-key"


def test_settings_clear_mt_key_falls_back_to_env(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update({"mt_api_key": "saved-override", "csrf_token": token})
    client.post("/settings", data=payload, follow_redirects=False)

    token = csrf(client, "/settings")
    cleared = dict(SETTINGS_FORM)
    cleared.update({"clear_mt_api_key": "on", "csrf_token": token})
    response = client.post("/settings", data=cleared, follow_redirects=False)
    assert response.status_code == 303

    from app.services import settings_store

    db = client.app.state.db
    assert settings_store.get_value(db, "mt_api_key") is None
    assert client.app.state.settings.mt_api_key == "test-key"
    assert client.app.state.mt._client.headers["x-api-key"] == "test-key"
    page = client.get("/settings")
    assert "已配置（环境变量）" in page.text


def test_settings_rejects_fill_and_clear_together(client: TestClient) -> None:
    login(client)
    token = csrf(client, "/settings")
    payload = dict(SETTINGS_FORM)
    payload.update(
        {
            "mt_api_key": "should-not-save",
            "clear_mt_api_key": "on",
            "csrf_token": token,
        }
    )
    response = client.post("/settings", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert "不能同时填写并清除" in response.text

    from app.services import settings_store

    assert settings_store.get_value(client.app.state.db, "mt_api_key") is None


def test_settings_saves_and_clears_qb_password(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, qb_username="admin", qb_password="env-qb-pass")
    app = create_app(settings, run_remote_checks=False)
    with TestClient(app) as test_client:
        login(test_client)
        token = csrf(test_client, "/settings")
        payload = dict(SETTINGS_FORM)
        payload.update({"qb_password": "new-qb-pass", "csrf_token": token})
        response = test_client.post("/settings", data=payload, follow_redirects=False)
        assert response.status_code == 303

        from app.services import settings_store

        db = test_client.app.state.db
        assert settings_store.get_value(db, "qb_password") == "new-qb-pass"
        assert test_client.app.state.settings.qb_password == "new-qb-pass"
        assert test_client.app.state.qb._logged_in is False
        page = test_client.get("/settings")
        assert "new-qb-pass" not in page.text
        assert "env-qb-pass" not in page.text

        token = csrf(test_client, "/settings")
        cleared = dict(SETTINGS_FORM)
        cleared.update({"clear_qb_password": "on", "csrf_token": token})
        test_client.post("/settings", data=cleared, follow_redirects=False)
        assert settings_store.get_value(db, "qb_password") is None
        assert test_client.app.state.settings.qb_password == "env-qb-pass"


def test_startup_uses_saved_credentials_when_env_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mt_api_key="")
    from app.services import credentials

    db = make_db(settings)
    credentials.save(db, credentials.MT_API_KEY, "db-only-key")

    app = create_app(settings, run_remote_checks=False)
    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200
        assert test_client.app.state.settings.mt_api_key == "db-only-key"
        assert test_client.app.state.mt._client.headers["x-api-key"] == "db-only-key"


def test_candidates_defaults_to_best_and_all_view(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    low = insert_torrent(
        db, sub_id, mt_id="low", title="示例剧.S01E02.720p-G", episode_start=2
    )
    high = insert_torrent(
        db, sub_id, mt_id="high", title="示例剧.S01E01.1080p-G", episode_start=1
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 50 WHERE id = ?", (low,))
        conn.execute("UPDATE torrent SET score = 90 WHERE id = ?", (high,))

    best = client.get("/candidates")
    assert best.status_code == 200
    assert "示例剧.S01E01.1080p-G" in best.text
    assert "示例剧.S01E02.720p-G" not in best.text
    assert "查看全部候选" in best.text

    all_rows = client.get("/candidates?view=all")
    assert all_rows.status_code == 200
    assert "示例剧.S01E01.1080p-G" in all_rows.text
    assert "示例剧.S01E02.720p-G" in all_rows.text


def test_candidates_filter_by_subscription(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_a = insert_subscription(db, title="剧集甲")
    sub_b = insert_subscription(db, title="剧集乙")
    insert_torrent(
        db, sub_a, mt_id="a1", title="剧集甲.S01E01.1080p-G", episode_start=1
    )
    insert_torrent(
        db, sub_b, mt_id="b1", title="剧集乙.S01E01.1080p-G", episode_start=1
    )

    page = client.get(f"/candidates?view=all&sub={sub_a}")
    assert page.status_code == 200
    assert "剧集甲.S01E01.1080p-G" in page.text
    assert "剧集乙.S01E01.1080p-G" not in page.text
    assert "全部订阅（2 条）" in page.text
    assert f'value="{sub_a}" selected' in page.text
    assert "清除筛选" in page.text

    best = client.get(f"/candidates?sub={sub_a}")
    assert "剧集甲.S01E01.1080p-G" in best.text
    assert "剧集乙.S01E01.1080p-G" not in best.text

    # An unknown filter is ignored instead of hiding everything.
    fallback = client.get("/candidates?view=all&sub=99999")
    assert "剧集甲.S01E01.1080p-G" in fallback.text
    assert "剧集乙.S01E01.1080p-G" in fallback.text


def test_candidates_actions_keep_subscription_filter(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_a = insert_subscription(db, title="剧集甲")
    sub_b = insert_subscription(db, title="剧集乙")
    torrent_id = insert_torrent(
        db, sub_a, mt_id="a1", title="剧集甲.S01E01.1080p-G", episode_start=1
    )
    insert_torrent(
        db, sub_b, mt_id="b1", title="剧集乙.S01E01.1080p-G", episode_start=1
    )
    token = csrf(client, "/tasks")
    response = client.post(
        f"/candidates/{torrent_id}/ignore",
        data={"csrf_token": token, "sub": str(sub_a), "view": "all"},
    )
    assert response.status_code == 200
    assert "剧集甲.S01E01.1080p-G" in response.text
    assert "剧集乙.S01E01.1080p-G" not in response.text


def test_candidates_batch_delete_is_scoped(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_a = insert_subscription(db, title="剧集甲")
    sub_b = insert_subscription(db, title="剧集乙")
    a_candidate = insert_torrent(db, sub_a, mt_id="a1", title="A.候选")
    a_ignored = insert_torrent(db, sub_a, mt_id="a2", title="A.忽略", state="ignored")
    b_candidate = insert_torrent(db, sub_b, mt_id="b1", title="B.候选")
    grabbed = insert_torrent(db, sub_a, mt_id="a3", title="A.已下载", state="grabbed")
    token = csrf(client, "/tasks")

    def remaining() -> set[int]:
        with db.read() as conn:
            return {
                int(row["id"])
                for row in conn.execute("SELECT id FROM torrent").fetchall()
            }

    response = client.post(
        "/candidates/delete",
        data={"csrf_token": token, "sub": str(sub_a), "view": "all"},
    )
    assert response.status_code == 200
    assert "已删除 1 条候选记录" in response.text
    assert remaining() == {a_ignored, b_candidate, grabbed}

    response = client.post(
        "/candidates/delete",
        data={
            "csrf_token": token,
            "sub": str(sub_a),
            "view": "all",
            "include_ignored": "1",
        },
    )
    assert response.status_code == 200
    assert "已删除 1 条候选记录（含已忽略）" in response.text
    assert remaining() == {b_candidate, grabbed}

    # No sub means every subscription, but downloaded tasks are never touched.
    response = client.post(
        "/candidates/delete", data={"csrf_token": token, "view": "best"}
    )
    assert response.status_code == 200
    assert "已删除 1 条候选记录" in response.text
    assert remaining() == {grabbed}
    assert a_candidate not in remaining()


def test_candidates_batch_delete_refuses_invalid_subscription(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="剧集甲")
    torrent_id = insert_torrent(db, sub_id, mt_id="a1", title="A.候选")
    token = csrf(client, "/tasks")
    response = client.post(
        "/candidates/delete", data={"csrf_token": token, "sub": "not-a-number"}
    )
    assert response.status_code == 200
    assert "订阅筛选无效，未删除任何记录" in response.text
    with db.read() as conn:
        row = conn.execute(
            "SELECT id FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
    assert row is not None


def test_candidates_batch_delete_requires_csrf(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="剧集甲")
    torrent_id = insert_torrent(db, sub_id, mt_id="a1", title="A.候选")
    response = client.post("/candidates/delete", data={"sub": str(sub_id)})
    assert response.status_code == 400
    with db.read() as conn:
        row = conn.execute(
            "SELECT id FROM torrent WHERE id = ?", (torrent_id,)
        ).fetchone()
    assert row is not None


def test_grab_best_route_uses_dry_run(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    torrent_id = insert_torrent(
        db, sub_id, mt_id="best", title="示例剧.S01E01.1080p-G", episode_start=1
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (torrent_id,))
    token = csrf(client, "/subscriptions")
    response = client.post(
        f"/subscriptions/{sub_id}/grab-best", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "DRY_RUN" in response.text


def test_best_view_shows_how_many_torrents_cover_aired_episodes(client: TestClient) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="示例剧")
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 2, available_episode = 2 WHERE id = ?",
            (sub_id,),
        )
    for episode in (1, 2):
        torrent_id = insert_torrent(
            db,
            sub_id,
            mt_id=f"e{episode}",
            title=f"示例剧.S01E{episode:02d}.1080p-G",
            episode_start=episode,
        )
        with db.tx() as conn:
            conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (torrent_id,))

    response = client.get("/candidates")
    assert response.status_code == 200
    assert "示例剧.S01E01.1080p-G" in response.text
    assert "示例剧.S01E02.1080p-G" in response.text
    assert "最优 1/2" in response.text
    assert "最优 2/2" in response.text
    assert "补集方案 2/2" in response.text
    assert "最优组合 2 个种子 · 按批次抓取" in response.text


def test_best_view_uses_multi_torrent_plan_and_grows_with_new_episodes(
    client: TestClient,
) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="长剧")
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 30, available_episode = 20 "
            "WHERE id = ?",
            (sub_id,),
        )
    first = insert_torrent(
        db,
        sub_id,
        mt_id="p18",
        title="长剧.S01E01-E18.1080p-G",
        episode_start=1,
        episode_end=18,
    )
    second = insert_torrent(
        db,
        sub_id,
        mt_id="p20",
        title="长剧.S01E19-E20.1080p-G",
        episode_start=19,
        episode_end=20,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 90 WHERE id = ?", (first,))
        conn.execute("UPDATE torrent SET score = 80 WHERE id = ?", (second,))

    page = client.get("/candidates")
    assert page.status_code == 200
    assert "长剧.S01E01-E18.1080p-G" in page.text
    assert "长剧.S01E19-E20.1080p-G" in page.text
    assert "最优 1/2" in page.text
    assert "最优 2/2" in page.text
    assert "补集方案 2/2" in page.text
    assert "最优组合 2 个种子 · 按批次抓取" in page.text

    # A newly aired episode with a matching release joins the best set.
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET available_episode = 21 WHERE id = ?", (sub_id,)
        )
    third = insert_torrent(
        db,
        sub_id,
        mt_id="p21",
        title="长剧.S01E21.1080p-G",
        episode_start=21,
        episode_end=21,
    )
    with db.tx() as conn:
        conn.execute("UPDATE torrent SET score = 70 WHERE id = ?", (third,))

    grown = client.get("/candidates")
    assert "长剧.S01E01-E18.1080p-G" in grown.text
    assert "长剧.S01E19-E20.1080p-G" in grown.text
    assert "长剧.S01E21.1080p-G" in grown.text
    for index in (1, 2, 3):
        assert f"最优 {index}/3" in grown.text
    assert "补集方案 3/3" in grown.text
    assert "最优组合 3 个种子 · 按批次抓取" in grown.text


def test_best_view_ignores_fake_complete_pack_and_shows_full_plan(
    client: TestClient,
) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="嫁金钗", year=2026)
    with db.tx() as conn:
        conn.execute(
            "UPDATE subscription SET total_episodes = 30, available_episode = 20 "
            "WHERE id = ?",
            (sub_id,),
        )
    ranges = [(1, 6), (7, 8), (9, 10), (11, 16), (17, 18), (19, 20)]
    for index, (start, end) in enumerate(ranges):
        torrent_id = insert_torrent(
            db,
            sub_id,
            mt_id=f"p{start}",
            title=f"嫁金钗.S01E{start:02d}-E{end:02d}.2160p-G",
            episode_start=start,
            episode_end=end,
        )
        with db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET score = ? WHERE id = ?", (90 - index, torrent_id)
            )
    # A single-episode upload mislabelled "S01 Complete" used to win alone.
    fake = insert_torrent(
        db,
        sub_id,
        mt_id="fake",
        title="嫁金钗.S01.Complete.2160p-G",
        episode_start=None,
    )
    with db.tx() as conn:
        conn.execute(
            "UPDATE torrent SET is_batch = 1, score = 99 WHERE id = ?", (fake,)
        )

    page = client.get("/candidates")
    assert page.status_code == 200
    for start, end in ranges:
        assert f"嫁金钗.S01E{start:02d}-E{end:02d}.2160p-G" in page.text
    assert "嫁金钗.S01.Complete.2160p-G" not in page.text
    for index in range(1, 7):
        assert f"最优 {index}/6" in page.text
    assert "最优组合 6 个种子 · 按批次抓取" in page.text


def test_subscription_delete_cleans_candidates_and_hides_tasks(
    client: TestClient,
) -> None:
    login(client)
    db = client.app.state.db  # type: ignore[attr-defined]
    sub_id = insert_subscription(db, title="已完成剧")
    other_id = insert_subscription(db, title="保留剧")
    candidate = insert_torrent(db, sub_id, mt_id="c1", title="已完成剧.候选")
    ignored = insert_torrent(
        db, sub_id, mt_id="i1", title="已完成剧.忽略", state="ignored"
    )
    task = insert_torrent(
        db, sub_id, mt_id="t1", title="已完成剧.S01E01.已整理", state="organized"
    )
    other_task = insert_torrent(
        db, other_id, mt_id="o1", title="保留剧.S01E01.已整理", state="organized"
    )
    token = csrf(client, "/subscriptions")

    before = client.get("/tasks?view=organized")
    assert "已完成剧.S01E01.已整理" in before.text

    response = client.post(
        f"/subscriptions/{sub_id}/delete", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "订阅已删除" in response.text
    assert "清理了 2 条候选" in response.text

    with db.read() as conn:
        sub_row = conn.execute(
            "SELECT deleted_at FROM subscription WHERE id = ?", (sub_id,)
        ).fetchone()
        remaining = {
            int(row["id"])
            for row in conn.execute("SELECT id FROM torrent").fetchall()
        }
    assert sub_row["deleted_at"]
    assert candidate not in remaining
    assert ignored not in remaining
    # Real tasks are retained so seeding cleanup can still manage them.
    assert task in remaining
    assert other_task in remaining

    after = client.get("/tasks?view=organized")
    assert "已完成剧.S01E01.已整理" not in after.text
    assert "保留剧.S01E01.已整理" in after.text

    candidates_page = client.get("/candidates?view=all")
    assert "已完成剧" not in candidates_page.text
