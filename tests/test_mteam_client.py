from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clients.mteam import MTeamClient
from app.config import Settings
from app.db import Database
from app.errors import (
    BudgetExceeded,
    MTeamApiError,
    MTeamAuthError,
    MTeamRateLimitError,
)
from app.services import settings_store
from app.services.ratelimit import ApiLimiter

SEARCH_DATA: dict[str, Any] = {
    "data": [
        {
            "id": "1001",
            "name": "Some.Show.S01E01.1080p.WEB-DL.H.264-GROUP",
            "smallDescr": "第一集",
            "category": {"id": "401", "name": "TV Series"},
            "size": 2_000_000_000,
            "numfiles": 3,
            "createdDate": "2026-10-01 12:00:00",
            "labelsNew": ["中字", {"name": "HDR"}],
            "imdb": "tt1234567",
            "imdbRating": "8.4",
            "douban": "https://movie.douban.com/subject/1",
            "doubanRating": 8,
            "status": {
                "seeders": "12",
                "leechers": 3,
                "timesCompleted": 40,
                "discount": "FREE",
                "discountEndTime": "2026-10-02 12:00:00",
                "visible": 1,
                "banned": 0,
            },
        }
    ],
    "total": 1,
    "pageNumber": 1,
    "pageSize": 100,
}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        mt_api_key="secret-key",
        mt_api_base="https://api.m-team.cc/api",
        mt_search_min_interval_sec=0,
        mt_search_daily_budget=100,
        mt_download_daily_budget=100,
        mt_profile_daily_budget=100,
    )


def make_client(
    tmp_path: Path,
    handler: Any,
    *,
    download_handler: Any | None = None,
) -> tuple[MTeamClient, ApiLimiter, list[httpx.Request]]:
    database = Database(tmp_path / "test.db")
    database.init()
    settings = make_settings(tmp_path)
    limiter = ApiLimiter(settings, database)
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        base_url=settings.mt_api_base,
        transport=httpx.MockTransport(recording_handler),
        headers={"x-api-key": settings.mt_api_key},
    )
    download_client = httpx.Client(
        transport=httpx.MockTransport(download_handler or handler),
        follow_redirects=True,
    )
    return MTeamClient(settings, limiter, client=client, download_client=download_client), limiter, seen


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "message": "SUCCESS", "data": data})


def test_search_parses_fields_and_sends_key(tmp_path: Path) -> None:
    client, limiter, seen = make_client(tmp_path, lambda request: ok(SEARCH_DATA))

    result = client.search("Some Show", page=1, page_size=100)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/torrent/search"
    assert request.headers["x-api-key"] == "secret-key"
    body = json.loads(request.content)
    assert body == {
        "keyword": "Some Show",
        "mode": "normal",
        "pageNumber": 1,
        "pageSize": 100,
        "visible": 1,
    }

    assert result.total == 1
    item = result.items[0]
    assert item.id == "1001"
    assert item.name == "Some.Show.S01E01.1080p.WEB-DL.H.264-GROUP"
    assert item.title == "第一集"
    assert item.description == "第一集"
    assert item.category == "TV Series"
    assert item.size_bytes == 2_000_000_000
    assert item.labels == ("中字", "HDR")
    assert item.imdb_rating == 8.4
    assert item.douban_rating == 8.0
    assert item.seeders == 12
    assert item.discount == "FREE"
    assert item.visible is True
    assert item.banned is False
    assert limiter.usage().search == 1


def test_search_sends_category_discount_and_sort_filters(tmp_path: Path) -> None:
    client, _, seen = make_client(tmp_path, lambda request: ok(SEARCH_DATA))

    result = client.search(
        "Some Show",
        categories=["419", "421"],
        discount="FREE",
        sort_field="SEEDERS",
        sort_direction="DESC",
    )

    body = json.loads(seen[0].content)
    assert body["categories"] == ["419", "421"]
    assert body["discount"] == "FREE"
    assert body["sortField"] == "SEEDERS"
    assert body["sortDirection"] == "DESC"
    assert result.total_pages == 1


def test_search_rejects_unknown_sort(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: ok(SEARCH_DATA))
    with pytest.raises(ValueError):
        client.search("x", sort_field="createdDate")
    with pytest.raises(ValueError):
        client.search("x", sort_direction="down")


def test_search_parses_total_pages(tmp_path: Path) -> None:
    data = {**SEARCH_DATA, "total": 10000, "totalPages": 100}
    client, _, _ = make_client(tmp_path, lambda request: ok(data))
    assert client.search("x", page_size=100).total_pages == 100


def test_search_list_shaped_data(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: ok(SEARCH_DATA["data"]))
    result = client.search("Some Show")
    assert result.total == 1
    assert result.items[0].id == "1001"
    assert result.page == 1
    assert result.page_size == 100


def test_search_empty_keyword_rejected_locally(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: ok(SEARCH_DATA))
    with pytest.raises(ValueError):
        client.search("x", page=0)
    with pytest.raises(ValueError):
        client.search("x", page_size=101)


def test_api_error_code_raises(tmp_path: Path) -> None:
    client, limiter, _ = make_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"code": "400", "message": "参数错误"}),
    )
    with pytest.raises(MTeamApiError) as excinfo:
        client.search("x")
    assert excinfo.value.code == "400"
    with limiter.db.read() as conn:
        assert conn.execute("SELECT ok FROM api_call").fetchone()["ok"] == 0


def test_auth_message_raises_auth_error(tmp_path: Path) -> None:
    client, _, _ = make_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"code": "401", "message": "invalid token"}),
    )
    with pytest.raises(MTeamAuthError):
        client.search("x")


def test_http_401_raises_auth_error(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: httpx.Response(401, text="unauthorized"))
    with pytest.raises(MTeamAuthError):
        client.search("x")


def test_cloudflare_403_is_rate_limit(tmp_path: Path) -> None:
    client, _, _ = make_client(
        tmp_path,
        lambda request: httpx.Response(403, text="error code: 1010"),
    )
    with pytest.raises(MTeamRateLimitError):
        client.search("x")


def test_http_429_is_rate_limit(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: httpx.Response(429))
    with pytest.raises(MTeamRateLimitError):
        client.search("x")


def test_5xx_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.mteam.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return ok(SEARCH_DATA)

    client, _, _ = make_client(tmp_path, handler)
    result = client.search("x")
    assert calls["n"] == 3
    assert len(result.items) == 1


def test_gen_download_url_posts_form(tmp_path: Path) -> None:
    client, limiter, seen = make_client(
        tmp_path,
        lambda request: ok("https://dl.m-team.cc/download/abc.torrent"),
    )
    url = client.gen_download_url("1001")
    assert url == "https://dl.m-team.cc/download/abc.torrent"
    request = seen[0]
    assert request.url.path == "/api/torrent/genDlToken"
    assert request.content == b"id=1001"
    assert limiter.usage().torrent == 1


def test_gen_download_url_rejects_bad_scheme(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, lambda request: ok("file:///etc/passwd"))
    with pytest.raises(MTeamApiError):
        client.gen_download_url("1001")


def test_download_torrent_has_no_api_key(tmp_path: Path) -> None:
    download_requests: list[httpx.Request] = []

    def download_handler(request: httpx.Request) -> httpx.Response:
        download_requests.append(request)
        return httpx.Response(200, content=b"d4:infod4:name5:hello6:lengthi1eee")

    client, _, _ = make_client(
        tmp_path,
        lambda request: ok(SEARCH_DATA),
        download_handler=download_handler,
    )
    data = client.download_torrent("https://dl.m-team.cc/download/abc.torrent")
    assert data.startswith(b"d4:info")
    assert "x-api-key" not in download_requests[0].headers
    assert "secret-key" not in str(download_requests[0].headers)


def test_download_size_limit(tmp_path: Path) -> None:
    client, _, _ = make_client(
        tmp_path,
        lambda request: httpx.Response(200, content=b"x" * 1024),
        download_handler=lambda request: httpx.Response(200, content=b"x" * 1024),
    )
    with pytest.raises(MTeamApiError):
        client.download_torrent("https://dl.m-team.cc/a.torrent", max_bytes=100)


def test_profile_parses_and_records(tmp_path: Path) -> None:
    payload = {
        "member": {
            "ratio": "1.75",
            "uploaded": "123456789",
            "downloaded": 70000000,
            "bonus": "123.5",
        }
    }
    client, limiter, seen = make_client(tmp_path, lambda request: ok(payload))

    profile = client.profile()

    assert seen[0].url.path == "/api/member/profile"
    assert profile.ratio == 1.75
    assert profile.uploaded == 123456789
    assert profile.downloaded == 70000000
    assert profile.bonus == 123.5
    assert limiter.usage().profile == 1


def test_profile_parses_live_member_count_shape(tmp_path: Path) -> None:
    payload = {
        "id": 341488,
        "username": "someone",
        "memberCount": {
            "shareRate": 1.395,
            "uploaded": 3804189736023,
            "downloaded": 2727132605636,
            "bonus": 665269.3,
        },
    }
    client, _, _ = make_client(tmp_path, lambda request: ok(payload))

    profile = client.profile()

    assert profile.ratio == 1.395
    assert profile.uploaded == 3804189736023
    assert profile.downloaded == 2727132605636
    assert profile.bonus == 665269.3


def test_budget_exhaustion_prevents_http_call(tmp_path: Path) -> None:
    client, limiter, seen = make_client(tmp_path, lambda request: ok(SEARCH_DATA))
    settings_store.set_values(limiter.db, {settings_store.SEARCH_DAILY_BUDGET_KEY: "0"})
    with pytest.raises(BudgetExceeded):
        client.search("x")
    assert seen == []
