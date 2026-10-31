from __future__ import annotations

import httpx
import pytest

from app.clients.douban import DoubanClient
from app.errors import DoubanError


def payload() -> dict[str, object]:
    return {
        "total": 2,
        "items": [
            {
                "id": "36675942",
                "title": "示例剧",
                "year": "2026",
                "card_subtitle": "2026 / 中国大陆 / 剧情",
                "episodes_info": "更新至 12 集",
                "rating": {"value": 8.2, "count": 1234},
                "pic": {"normal": "https://img.example/poster.jpg"},
            },
            {
                "id": "1234567",
                "title": "示例电影",
                "year": "2025",
                "card_subtitle": "2025 / 美国 / 科幻",
                "rating": {"value": 7.5, "count": 99},
                "pic": {"normal": "javascript:alert(1)"},
            },
        ],
    }


def test_fetch_parses_and_caches() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        first = douban.fetch("tv", "U", count=20)
        second = douban.fetch("tv", "U", count=20)

    assert len(calls) == 1
    assert first.total == 2
    assert first.has_more is False
    assert first.cached is False
    assert second.cached is True
    item = first.items[0]
    assert item.id == "36675942"
    assert item.kind == "tv"
    assert item.title == "示例剧"
    assert item.year == 2026
    assert item.rating == 8.2
    assert item.rating_count == 1234
    assert item.episodes_info == "更新至 12 集"
    assert item.cover_url == "https://img.example/poster.jpg"
    assert item.url == "https://movie.douban.com/subject/36675942/"
    assert item.rank == 1
    assert first.items[1].cover_url is None


def test_rexxar_chart_reports_more_pages() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"total": 40, "items": payload()["items"]})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch("tv", "U", start=20, count=2)

    assert len(calls) == 1
    assert calls[0].url.params["start"] == "20"
    assert calls[0].url.params["count"] == "2"
    assert chart.has_more is True
    assert chart.items[0].rank == 21


def test_variety_chart_uses_recommend_with_type_tag() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "total": 60,
                "items": [
                    {
                        "id": "36540851",
                        "title": "示例综艺",
                        "type": "tv",
                        "year": "2024",
                        "rating": {"value": 8.8, "count": 77},
                        "card_subtitle": "2024 / 中国大陆 / 真人秀",
                        "pic": {"normal": "https://img.example/variety.jpg"},
                    },
                    {
                        "id": "36247293",
                        "title": "示例综艺二",
                        "type": "tv",
                        "year": "2023",
                        "pic": {"normal": "javascript:alert(1)"},
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch("variety", "U", start=20, count=20)

    assert len(calls) == 1
    request = calls[0]
    assert request.url.host == "m.douban.com"
    assert request.url.path == "/rexxar/api/v2/tv/recommend"
    assert request.url.params["tags"] == "综艺"
    assert request.url.params["sort"] == "U"
    assert request.url.params["start"] == "20"
    assert request.url.params["count"] == "20"
    assert "tag" not in request.url.params
    assert "page_start" not in request.url.params
    assert chart.kind == "variety"
    assert chart.total == 60
    assert chart.has_more is True
    first = chart.items[0]
    assert first.id == "36540851"
    assert first.title == "示例综艺"
    assert first.kind == "tv"
    assert first.year == 2024
    assert first.rating == 8.8
    assert first.rating_count == 77
    assert first.subtitle == "2024 / 中国大陆 / 真人秀"
    assert first.cover_url == "https://img.example/variety.jpg"
    assert first.rank == 21
    assert chart.items[1].cover_url is None
    assert chart.items[1].rating is None


def test_variety_chart_short_page_has_no_more() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 0, "items": []})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch("variety", "S", count=20)

    assert chart.items == ()
    assert chart.has_more is False


def test_empty_page_ends_pagination_even_with_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 500, "items": []})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch("tv", "U", start=100, count=20)

    assert chart.items == ()
    assert chart.total == 500
    assert chart.has_more is False


def test_bad_payload_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": "not-a-list"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http)
        with pytest.raises(DoubanError):
            douban.fetch()


def test_network_error_uses_stale_cache() -> None:
    responses = [
        httpx.Response(200, json=payload()),
        httpx.Response(500, text="blocked"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        douban.fetch()
        stale = douban.fetch(refresh=True)

    assert stale.cached is True
    assert stale.stale is True
    assert stale.items[0].title == "示例剧"


def test_bad_payload_uses_stale_cache() -> None:
    responses = [
        httpx.Response(200, json=payload()),
        httpx.Response(200, json={"items": "not-a-list"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        douban.fetch()
        stale = douban.fetch(refresh=True)

    assert stale.stale is True
    assert stale.items[0].title == "示例剧"


def filtered_payload(
    count: int = 2,
    *,
    total: int = 60,
    item_type: str = "movie",
    year: str = "2024",
) -> dict[str, object]:
    return {
        "total": total,
        "items": [
            {
                "id": str(36000000 + index),
                "title": f"筛选示例{index}",
                "type": item_type,
                "year": year,
                "rating": {"value": 8.1, "count": 4321},
                "card_subtitle": f"{year} / 中国大陆 / 喜剧",
                "episodes_info": "",
                "pic": {"normal": f"https://img.example/{index}.jpg"},
            }
            for index in range(count)
        ],
    }


def test_filtered_chart_uses_recommend_tags() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=filtered_payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch(
            "movie", "S", start=20, count=20, year="2024", genre="喜剧"
        )

    assert len(calls) == 1
    request = calls[0]
    assert request.url.host == "m.douban.com"
    assert request.url.path == "/rexxar/api/v2/movie/recommend"
    assert request.url.params["sort"] == "S"
    assert request.url.params["start"] == "20"
    assert request.url.params["count"] == "20"
    assert request.url.params["tags"] == "喜剧,2024"
    assert request.url.params["selected_categories"] == "{}"
    assert "data" not in request.url.params
    assert chart.total == 60
    assert chart.has_more is True
    item = chart.items[0]
    assert item.id == "36000000"
    assert item.kind == "movie"
    assert item.title == "筛选示例0"
    assert item.year == 2024
    assert item.rating == 8.1
    assert item.rating_count == 4321
    assert item.subtitle == "2024 / 中国大陆 / 喜剧"
    assert item.cover_url == "https://img.example/0.jpg"
    assert item.rank == 21


def test_filtered_chart_uses_decade_tag_and_item_year() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json=filtered_payload(item_type="tv", year="2013")
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch("tv", "U", year="2010年代")

    assert calls[0].url.path == "/rexxar/api/v2/tv/recommend"
    assert calls[0].url.params["tags"] == "2010年代"
    assert chart.items[0].year == 2013


def test_large_page_is_split_into_fifty_item_requests() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        start = int(request.url.params["start"])
        count = int(request.url.params["count"])
        return httpx.Response(
            200,
            json={
                "total": 500,
                "items": [
                    {
                        "id": str(40000000 + start + index),
                        "title": f"条目{start + index}",
                        "type": "tv",
                        "year": "2024",
                    }
                    for index in range(count)
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch(
            "tv", "U", count=100, genre="纪录片"
        )

    assert [call.url.params["count"] for call in calls] == ["50", "50"]
    assert [call.url.params["start"] for call in calls] == ["0", "50"]
    assert len(chart.items) == 100
    assert chart.items[0].title == "条目0"
    assert chart.items[99].title == "条目99"
    assert chart.total == 500
    assert chart.has_more is True


def test_filtered_chart_drops_unknown_filters() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        DoubanClient(client=http).fetch("tv", "U", year="2999", genre="<script>")

    assert calls[0].url.path == "/rexxar/api/v2/tv/recommend"
    assert "tags" not in calls[0].url.params


def test_filtered_chart_variety_adds_type_tag() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=filtered_payload(item_type="tv"))

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch(
            "variety", "U", year="2024", genre="真人秀"
        )

    assert calls[0].url.path == "/rexxar/api/v2/tv/recommend"
    assert calls[0].url.params["tags"] == "综艺,真人秀,2024"
    assert chart.items[0].kind == "tv"
    assert chart.items[0].year == 2024


def test_filtered_chart_skips_chart_and_cross_type_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 3,
                "items": [
                    {
                        "id": "film_genre_32",
                        "title": "高分经典悬疑片榜",
                        "type": "chart",
                    },
                    {
                        "id": "111",
                        "title": "悬疑电影",
                        "type": "movie",
                        "year": "2024",
                        "rating": {"value": 8.0, "count": 10},
                    },
                    {
                        "id": "222",
                        "title": "悬疑剧集",
                        "type": "tv",
                        "year": "2024",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http).fetch(
            "movie", "U", genre="悬疑", count=3
        )

    assert [item.title for item in chart.items] == ["悬疑电影"]
    assert chart.total == 3
    assert chart.has_more is False


def test_filtered_and_unfiltered_charts_have_separate_cache_entries() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "tags" in request.url.params:
            return httpx.Response(200, json=filtered_payload())
        return httpx.Response(200, json=payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        unfiltered = douban.fetch("movie", "U")
        filtered = douban.fetch("movie", "U", year="2024")
        cached = douban.fetch("movie", "U", year="2024")

    assert len(calls) == 2
    assert unfiltered.items[0].title == "示例剧"
    assert filtered.items[0].title == "筛选示例0"
    assert cached.cached is True


def test_fetch_episodes_reads_total_and_airing_progress() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/rexxar/api/v2/tv/36675942"
        return httpx.Response(
            200,
            json={
                "episodes_count": 29,
                "episodes_info": "更新至14集",
                "last_episode_number": None,
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        first = douban.fetch_episodes("https://movie.douban.com/subject/36675942/")
        second = douban.fetch_episodes("36675942")

    assert len(calls) == 1
    assert first.total == 29
    assert first.available == 14
    assert first.info == "更新至14集"
    assert first.cached is False
    assert second.cached is True


def test_fetch_episodes_prefers_last_episode_number() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "episodes_count": 40,
                "episodes_info": "更新至10集",
                "last_episode_number": 12,
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        episodes = DoubanClient(client=http).fetch_episodes("35861007")

    assert episodes.total == 40
    assert episodes.available == 12


def test_network_error_without_cache_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http)
        with pytest.raises(DoubanError):
            douban.fetch()
