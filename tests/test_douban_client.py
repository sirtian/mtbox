from __future__ import annotations

import httpx
import pytest

from app.clients.douban import DoubanClient, normalize_cover_url
from app.errors import DoubanError

COVER = "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p2936709341.jpg"


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
                "pic": {"normal": COVER},
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
    assert item.cover_url == COVER
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
                        "pic": {"normal": COVER},
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
    assert first.cover_url == COVER
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
                "pic": {
                    "normal": (
                        "https://img2.doubanio.com/view/photo/"
                        f"s_ratio_poster/public/p{index}.jpg"
                    )
                },
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
    assert item.cover_url == (
        "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p0.jpg"
    )
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


def test_normalize_cover_url_only_accepts_douban_posters() -> None:
    assert normalize_cover_url(COVER) == COVER
    assert normalize_cover_url(
        "https://img9.doubanio.com/view/photo/l_ratio_poster/public/p1.webp"
    )
    for value in (
        "http://img2.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg",
        "https://img.example/view/photo/s_ratio_poster/public/p1.jpg",
        "https://img2.doubanio.com/not-a-poster.jpg",
        "https://img2.doubanio.com/view/photo/../secret.jpg",
        "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg?x=1",
        "https://user:pass@img2.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg",
        "https://img2.doubanio.com:8443/view/photo/s_ratio_poster/public/p1.jpg",
        "javascript:alert(1)",
        "",
        None,
    ):
        assert normalize_cover_url(value) is None


def test_fetch_cover_uses_referer_and_caches() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, content=b"\xff\xd8poster", headers={"content-type": "image/jpeg"}
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        first = douban.fetch_cover(COVER)
        second = douban.fetch_cover(COVER)

    assert first == (b"\xff\xd8poster", "image/jpeg")
    assert second == first
    assert len(calls) == 1
    assert calls[0].url.host == "img2.doubanio.com"
    assert calls[0].url.path == "/view/photo/s_ratio_poster/public/p2936709341.jpg"
    assert calls[0].headers["referer"] == "https://m.douban.com/"
    assert calls[0].headers["accept"].startswith("image/")


def test_fetch_cover_rejects_invalid_url_without_network() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"x")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http)
        with pytest.raises(DoubanError):
            douban.fetch_cover("https://example.com/poster.jpg")

    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://example.com/poster.jpg"}),
        httpx.Response(
            200,
            text="<html>blocked</html>",
            headers={"content-type": "text/html"},
        ),
        httpx.Response(
            200, content=b"x", headers={"content-type": "image/svg+xml"}
        ),
        httpx.Response(
            200,
            content=b"x" * (2 * 1024 * 1024 + 1),
            headers={"content-type": "image/jpeg"},
        ),
        httpx.Response(
            200, content=b"", headers={"content-type": "image/jpeg"}
        ),
    ],
)
def test_fetch_cover_rejects_bad_upstream(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http)
        with pytest.raises(DoubanError):
            douban.fetch_cover(COVER)


SIGNED_COVER = (
    "https://qnmob3-sign.doubanio.com/view/photo/large/public/p1.jpg"
    "?sa_cv=abc123&sa_ct=456&imageView2/0/q/80/w/9999/h/120/format/jpg"
)


def search_payload() -> dict[str, object]:
    return {
        "subjects": {
            "total": 3,
            "start": 0,
            "count": 20,
            "items": [
                {
                    "target_type": "tv",
                    "target_id": "36942992",
                    "target": {
                        "id": "36942992",
                        "title": "走马上任",
                        "year": "2026",
                        "card_subtitle": "中国大陆 / 剧情 喜剧",
                        "rating": {"value": 0, "count": 0},
                        "cover_url": SIGNED_COVER,
                    },
                },
                {
                    "target_type": "chart",
                    "target": {"id": "tv_hot", "title": "实时热门电视"},
                },
                {
                    "target_type": "movie",
                    "target_id": "123",
                    "target": {
                        "id": "123",
                        "title": "示例电影",
                        "year": "2024",
                        "card_subtitle": "美国 / 科幻",
                        "rating": {"value": 7.5, "count": 10},
                    },
                },
            ],
        }
    }


def test_search_reads_site_wide_results_and_caches() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=search_payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=600)
        first = douban.search("tv", "走马上任")
        second = douban.search("tv", "走马上任")

    assert len(calls) == 1
    assert calls[0].url.path == "/rexxar/api/v2/search/subjects"
    assert calls[0].url.params["q"] == "走马上任"
    assert calls[0].url.params["start"] == "0"
    assert calls[0].url.params["count"] == "20"
    assert first.total == 3
    assert second.cached is True
    assert len(first.items) == 1
    item = first.items[0]
    assert item.id == "36942992"
    assert item.title == "走马上任"
    assert item.kind == "tv"
    assert item.year == 2026
    assert item.rating is None
    assert item.cover_url == SIGNED_COVER
    assert item.rank == 1


def test_search_filters_by_requested_kind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        douban = DoubanClient(client=http, cache_sec=0)
        movies = douban.search("movie", "示例")
        variety = douban.search("variety", "走马上任")

    assert [item.id for item in movies.items] == ["123"]
    assert movies.items[0].kind == "movie"
    assert [item.id for item in variety.items] == ["36942992"]


def test_search_rejects_empty_keyword() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="https://m.douban.com",
    ) as http:
        with pytest.raises(ValueError):
            DoubanClient(client=http).search("tv", "   ")


def test_search_wraps_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        with pytest.raises(DoubanError):
            DoubanClient(client=http).search("tv", "示例")


def test_normalize_cover_url_accepts_signed_search_covers() -> None:
    assert normalize_cover_url(SIGNED_COVER) == SIGNED_COVER
    for value in (
        "https://qnmob3-sign.doubanio.com/view/photo/large/public/p1.jpg",
        "https://qnmob3-sign.doubanio.com/view/photo/large/public/p1.jpg?x=<script>",
        "https://qnmob3-sign.doubanio.com/view/photo/large/public/p1.jpg?x=1#frag",
        "https://qnmob3-sign.doubanio.com/not-a-poster.jpg?x=1",
        "http://qnmob3-sign.doubanio.com/view/photo/large/public/p1.jpg?x=1",
        "https://evil.example/view/photo/large/public/p1.jpg?x=1",
    ):
        assert normalize_cover_url(value) is None


def _chart_item(index: int) -> dict[str, object]:
    return {
        "id": str(30000000 + index),
        "title": f"示例{index}",
        "year": "2026",
        "card_subtitle": "2026 / 中国大陆 / 剧情",
        "rating": {"value": 8.0, "count": 10},
        "pic": {"normal": COVER},
    }


def test_scan_reads_the_whole_filtered_chart() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        start = int(request.url.params["start"])
        return httpx.Response(
            200,
            json={"total": 120, "items": [_chart_item(start + i) for i in range(50)]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http, cache_sec=0).scan(
            "tv", "U", year="2026", genre="剧情", limit=100
        )

    assert len(calls) == 2
    assert calls[0].url.path == "/rexxar/api/v2/tv/recommend"
    assert calls[0].url.params["tags"] == "剧情,2026"
    assert calls[0].url.params["count"] == "50"
    assert calls[1].url.params["start"] == "50"
    assert chart.total == 120
    assert len(chart.items) == 100
    assert chart.items[0].rank == 1
    assert chart.items[-1].rank == 100
    assert chart.cached is False


def test_scan_stops_at_the_end_of_the_chart() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"total": 3, "items": [_chart_item(0)]})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://m.douban.com") as http:
        chart = DoubanClient(client=http, cache_sec=0).scan("tv", "U", limit=500)

    assert len(calls) == 1
    assert len(chart.items) == 1
    assert chart.total == 3
