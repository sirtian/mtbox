"""Douban public chart client.

Uses the public mobile chart JSON, which needs no account, cookie or API key.
Responses are cached briefly so opening the page repeatedly does not hammer
Douban.  The data is treated as untrusted input: only a small whitelist of
fields is kept, and every text field is length-limited.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import DoubanError
from ..timeutil import to_iso, utc_now

DOUBAN_KINDS = {"tv": "剧集", "movie": "电影", "variety": "综艺"}
DOUBAN_SORTS = {"U": "近期热门", "R": "最新上线", "S": "高分优先"}

# Filtered charts reuse the rexxar recommend endpoint with a comma-separated
# ``tags`` list (e.g. ``悬疑,2024``).  The web "explore" endpoint's genre index
# is sparse for TV (``电视剧`` + ``纪录片`` returns only a couple of entries),
# while the recommend tags cover the whole catalogue and also report a total.
# Douban's own tag text is sent verbatim, hence the fixed whitelists below.
_MOVIE_GENRES = (
    "喜剧", "爱情", "动作", "科幻", "动画", "悬疑", "惊悚", "恐怖", "剧情",
    "纪录片", "短片", "情色", "音乐", "歌舞", "家庭", "儿童", "传记",
    "历史", "战争", "犯罪", "西部", "奇幻", "冒险", "灾难", "武侠", "古装",
)
_VARIETY_GENRES = (
    "真人秀", "脱口秀", "访谈", "选秀", "生活", "音乐", "竞技", "喜剧",
    "情感", "文化", "旅行", "美食", "推理", "游戏",
)
ALL_LABEL = "全部"

# Douban's year filters are labels rather than numbers for older periods.
_YEAR_RANGES = {
    "2020年代": (2020, 2029),
    "2010年代": (2010, 2019),
    "2000年代": (2000, 2009),
    "90年代": (1990, 1999),
    "80年代": (1980, 1989),
    "70年代": (1970, 1979),
    "60年代": (1960, 1969),
    "更早": (1900, 1959),
}

_BASE_URL = "https://m.douban.com"
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Douban's image CDN rejects requests without a douban.com Referer (418/403),
# so the browser cannot load poster URLs directly.  The UI asks the app for
# /douban/cover and this client fetches the image with the required header.
# Only the official CDN hosts and poster path shape are accepted: the URL
# comes from an untrusted upstream payload and must never become an open proxy.
_COVER_HOSTS = frozenset(
    {f"img{n}.doubanio.com" for n in range(1, 10)} | {"img.doubanio.com"}
)
_COVER_PATH = re.compile(
    r"^/view/photo/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)$"
)
_COVER_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_COVER_MAX_BYTES = 2 * 1024 * 1024
# The mobile search API returns signed poster URLs on rotating
# ``qnmob<N>-sign.doubanio.com`` hosts.  The signature lives in the query
# string, so these hosts are accepted with a strictly validated query; plain
# CDN hosts still must not carry one.
_SIGNED_COVER_HOST = re.compile(r"^qnmob\d+-sign\.doubanio\.com$")
_SIGNED_COVER_QUERY = re.compile(r"^[A-Za-z0-9_=&%,./:+-]{1,300}$")
_COVER_CACHE_TTL = 6 * 3600
_COVER_CACHE_MAX_ITEMS = 256
_COVER_CACHE_MAX_BYTES = 32 * 1024 * 1024


def normalize_cover_url(value: Any) -> str | None:
    """Return a safe Douban CDN poster URL, or ``None``.

    The chart payload is untrusted.  Reject anything that is not an HTTPS
    poster on a known Douban image host, with no credentials, query or
    fragment that could redirect the proxy elsewhere.
    """
    if not isinstance(value, str) or not value or len(value) > 500:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = parts.netloc.lower()
    signed_host = bool(_SIGNED_COVER_HOST.fullmatch(host))
    if parts.scheme != "https" or (host not in _COVER_HOSTS and not signed_host):
        return None
    if parts.fragment or parts.username or parts.password:
        return None
    if signed_host:
        if not _SIGNED_COVER_QUERY.fullmatch(parts.query):
            return None
    elif parts.query:
        return None
    if not _COVER_PATH.fullmatch(parts.path):
        return None
    segments = parts.path.lstrip("/").split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return None
    return value


@dataclass(frozen=True, slots=True)
class DoubanItem:
    id: str
    kind: str
    title: str
    year: int | None
    rating: float | None
    rating_count: int
    subtitle: str
    episodes_info: str
    cover_url: str | None
    url: str
    rank: int


@dataclass(frozen=True, slots=True)
class DoubanChart:
    kind: str
    sort: str
    items: tuple[DoubanItem, ...]
    total: int
    fetched_at: str
    cached: bool = False
    stale: bool = False
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class DoubanSearch:
    """One page of Douban's site-wide subject search."""

    q: str
    kind: str
    items: tuple[DoubanItem, ...]
    total: int
    start: int
    count: int
    fetched_at: str
    cached: bool = False
    stale: bool = False
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class DoubanEpisodes:
    subject_id: str
    total: int | None
    available: int | None
    info: str
    fetched_at: str
    cached: bool = False
    stale: bool = False
    original_title: str = ""
    aliases: tuple[str, ...] = ()
    year: int | None = None


_EPISODE_INFO = re.compile(r"(?:更新至|已更新至)?\s*第?\s*(\d{1,4})\s*[集话話期]")


def genre_options(kind: str) -> tuple[str, ...]:
    """Selectable genres for a chart kind; "全部" means no filter."""
    genres = _VARIETY_GENRES if kind == "variety" else _MOVIE_GENRES
    return (ALL_LABEL, *genres)


def year_options(now: datetime | None = None) -> tuple[str, ...]:
    """Year filter choices: recent years plus Douban's period labels."""
    current = (now or utc_now()).year
    recent = tuple(str(value) for value in range(current, 2019, -1))
    return (ALL_LABEL, *recent, *_YEAR_RANGES)


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _digits(value: Any, length: int = 0) -> str:
    text = "".join(ch for ch in _text(value, 100) if ch.isdigit())
    if length:
        return text[:length]
    return text


def _year(value: Any) -> int | None:
    digits = _digits(value, 4)
    if len(digits) != 4:
        return None
    year = int(digits)
    return year if 1800 <= year <= 2200 else None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 10 else None


def _int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _episode_number(value: Any) -> int | None:
    text = _text(value, 100)
    match = _EPISODE_INFO.search(text)
    if not match:
        return None
    number = _int(match.group(1), 0)
    return number if 0 < number <= 1000 else None


def _cover(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("normal", "large"):
        value = normalize_cover_url(raw.get(key))
        if value:
            return value
    return None


def _search_item(raw: Any, kind: str, rank: int) -> DoubanItem | None:
    """Parse one ``/search/subjects`` row, keeping only film/TV subjects."""
    if not isinstance(raw, dict):
        return None
    target_type = _text(raw.get("target_type"), 20)
    if target_type == "movie":
        item_kind = "movie"
    elif target_type == "tv":
        item_kind = "tv"
    else:
        # Charts, books and podcasts cannot be turned into a subscription.
        return None
    if kind == "movie" and item_kind != "movie":
        return None
    if kind in ("tv", "variety") and item_kind != "tv":
        return None
    target = raw.get("target")
    if not isinstance(target, dict):
        return None
    item_id = _digits(target.get("id") or raw.get("target_id"), 20)
    title = _text(target.get("title"), 200)
    if not item_id or not title:
        return None
    rating = target.get("rating") if isinstance(target.get("rating"), dict) else {}
    rating_value = _float(rating.get("value"))
    rating_count = _int(rating.get("count"))
    if rating_value == 0 or not rating_count:
        rating_value = None
    return DoubanItem(
        id=item_id,
        kind=item_kind,
        title=title,
        year=_year(target.get("year")),
        rating=rating_value,
        rating_count=rating_count,
        subtitle=_text(target.get("card_subtitle"), 500),
        episodes_info=_text(target.get("episodes_info"), 100),
        cover_url=normalize_cover_url(target.get("cover_url")),
        url=f"https://movie.douban.com/subject/{item_id}/",
        rank=rank,
    )


class DoubanClient:
    def __init__(
        self,
        *,
        timeout_sec: float = 15.0,
        cache_sec: int = 600,
        trust_env: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.cache_sec = max(0, int(cache_sec))
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Accept": "application/json",
                "Referer": "https://m.douban.com/",
                "User-Agent": _MOBILE_UA,
            },
            timeout=self.timeout_sec,
            follow_redirects=True,
            trust_env=trust_env,
        )
        self._cache: dict[tuple[str, str, int, int, str], tuple[float, DoubanChart]] = {}
        self._search_cache: dict[
            tuple[str, str, int, int], tuple[float, DoubanSearch]
        ] = {}
        self._episode_cache: dict[str, tuple[float, DoubanEpisodes]] = {}
        self._cover_cache: dict[str, tuple[float, bytes, str]] = {}
        self._cover_cache_bytes = 0
        self._cover_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DoubanClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_episodes(
        self, subject_id: str, *, refresh: bool = False
    ) -> DoubanEpisodes:
        """Read the total/current episode count for a TV subject.

        The chart API only tells whether a show is airing ("更新至 14 集").  The
        subject endpoint additionally exposes `episodes_count`, which is the
        planned season total we need for the progress display.
        """
        sid = _digits(subject_id, 20)
        if not sid:
            raise DoubanError("豆瓣 ID 为空")
        now = time.monotonic()
        # Episode totals change slowly; keep them longer than chart responses
        # so a subscription check does not query Douban every 30 minutes.
        ttl = max(self.cache_sec, 3600) if self.cache_sec else 0
        if not refresh and ttl:
            cached = self._episode_cache.get(sid)
            if cached and now - cached[0] < ttl:
                return replace(cached[1], cached=True)

        try:
            response = self._client.get(f"/rexxar/api/v2/tv/{sid}")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            cached = self._episode_cache.get(sid)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise DoubanError(f"豆瓣剧集信息读取失败: {exc}") from exc

        if not isinstance(payload, dict):
            cached = self._episode_cache.get(sid)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise DoubanError("豆瓣剧集信息返回了无法识别的数据")

        total = _int(payload.get("episodes_count"), 0)
        if not 0 < total <= 1000:
            total = 0
        info = _text(payload.get("episodes_info"), 100)
        available = _int(payload.get("last_episode_number"), 0)
        if not 0 < available <= 1000:
            available = _episode_number(info) or 0
        aliases: list[str] = []
        raw_aka = payload.get("aka")
        if isinstance(raw_aka, list):
            for value in raw_aka:
                alias = _text(value, 100)
                if alias and alias not in aliases:
                    aliases.append(alias)
                if len(aliases) >= 10:
                    break
        episodes = DoubanEpisodes(
            subject_id=sid,
            total=total or None,
            available=available or None,
            info=info,
            fetched_at=to_iso(utc_now()),
            original_title=_text(payload.get("original_title"), 200),
            aliases=tuple(aliases),
            year=_year(payload.get("year")),
        )
        if self.cache_sec:
            self._episode_cache[sid] = (now, episodes)
        return episodes

    def fetch_cover(self, url: str) -> tuple[bytes, str]:
        """Fetch a poster through the required Douban Referer.

        Returns ``(image_bytes, media_type)``.  Responses are capped at 2 MiB
        and cached in memory for a few hours; the browser also caches them for
        a day.  Invalid URLs are rejected before any network request.
        """
        normalized = normalize_cover_url(url)
        if normalized is None:
            raise DoubanError("无效的豆瓣海报地址")
        now = time.monotonic()
        with self._cover_lock:
            cached = self._cover_cache.get(normalized)
            if cached and now - cached[0] < _COVER_CACHE_TTL:
                return cached[1], cached[2]

        try:
            with self._client.stream(
                "GET",
                normalized,
                headers={
                    "Accept": "image/webp,image/png,image/jpeg;q=0.9,*/*;q=0.8",
                    "Referer": "https://m.douban.com/",
                    "User-Agent": _MOBILE_UA,
                },
                follow_redirects=False,
                timeout=min(self.timeout_sec, 10.0),
            ) as response:
                if response.status_code != 200:
                    raise DoubanError(
                        f"豆瓣海报读取失败: HTTP {response.status_code}"
                    )
                media_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if media_type not in _COVER_TYPES:
                    raise DoubanError("豆瓣海报返回了非图片内容")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _COVER_MAX_BYTES:
                        raise DoubanError("豆瓣海报文件过大")
        except httpx.HTTPError as exc:
            raise DoubanError(f"豆瓣海报读取失败: {exc}") from exc

        data = bytes(body)
        if not data:
            raise DoubanError("豆瓣海报为空")
        with self._cover_lock:
            previous = self._cover_cache.pop(normalized, None)
            if previous:
                self._cover_cache_bytes -= len(previous[1])
            self._cover_cache[normalized] = (now, data, media_type)
            self._cover_cache_bytes += len(data)
            while (
                len(self._cover_cache) > _COVER_CACHE_MAX_ITEMS
                or self._cover_cache_bytes > _COVER_CACHE_MAX_BYTES
            ):
                oldest = next(iter(self._cover_cache))
                _, evicted, _ = self._cover_cache.pop(oldest)
                self._cover_cache_bytes -= len(evicted)
        return data, media_type

    # The recommend endpoint silently caps a single response at 50 entries.
    _RECOMMEND_CHUNK = 50

    def _recommend_items(
        self, kind: str, sort: str, start: int, count: int, tags: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Read up to ``count`` recommend entries, splitting large pages.

        A 100-row page is assembled from two 50-row requests.  Entries are
        de-duplicated by subject id (the endpoint can repeat the tail), and
        ``total`` is taken from the first response.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        offset = start
        remaining = count
        first = True
        while remaining > 0:
            take = min(self._RECOMMEND_CHUNK, remaining)
            params: dict[str, object] = {
                "refresh": 0,
                "start": offset,
                "count": take,
                "selected_categories": "{}",
                "uncollect": "false",
                "sort": sort,
            }
            if tags:
                params["tags"] = tags
            response = self._client.get(
                f"/rexxar/api/v2/{kind}/recommend", params=params
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise DoubanError("豆瓣榜单返回了无法识别的数据")
            items = payload.get("items")
            if not isinstance(items, list):
                raise DoubanError("豆瓣榜单返回了无法识别的数据")
            if first:
                total = _int(payload.get("total"), 0)
                first = False
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                item_id = _digits(raw.get("id"), 20)
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                collected.append(raw)
            offset += len(items)
            remaining -= len(items)
            if len(items) < take:
                break
        return collected, total

    def search(
        self,
        kind: str,
        q: str,
        *,
        start: int = 0,
        count: int = 20,
        refresh: bool = False,
    ) -> DoubanSearch:
        """Search Douban's whole film/TV catalogue by keyword.

        The chart endpoint has no keyword parameter, so filtering a single
        fetched chart page misses titles that are on another page, another
        sort order or not in the chart at all.  The mobile search endpoint
        covers the full catalogue and returns the same subject fields.
        """
        if kind not in DOUBAN_KINDS:
            raise ValueError(f"未知豆瓣类型: {kind}")
        query = " ".join(str(q or "").split())[:50]
        if not query:
            raise ValueError("搜索关键字不能为空")
        start = max(0, int(start))
        count = min(100, max(1, int(count)))
        key = (kind, query, start, count)
        now = time.monotonic()
        if not refresh and self.cache_sec:
            cached = self._search_cache.get(key)
            if cached and now - cached[0] < self.cache_sec:
                return replace(cached[1], cached=True)

        try:
            # The search endpoint also caps one response at 50 rows.
            raw_items: list[Any] = []
            total = 0
            offset = start
            remaining = count
            first = True
            while remaining > 0:
                take = min(self._RECOMMEND_CHUNK, remaining)
                response = self._client.get(
                    "/rexxar/api/v2/search/subjects",
                    params={"q": query, "start": offset, "count": take},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise DoubanError("豆瓣搜索返回了无法识别的数据")
                subjects = payload.get("subjects")
                if not isinstance(subjects, dict):
                    raise DoubanError("豆瓣搜索返回了无法识别的数据")
                page_items = subjects.get("items")
                if not isinstance(page_items, list):
                    raise DoubanError("豆瓣搜索返回了无法识别的数据")
                if first:
                    total = _int(subjects.get("total"), 0)
                    first = False
                raw_items.extend(page_items)
                offset += len(page_items)
                remaining -= len(page_items)
                if len(page_items) < take:
                    break
        except DoubanError:
            cached = self._search_cache.get(key)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            cached = self._search_cache.get(key)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise DoubanError(f"豆瓣搜索失败: {exc}") from exc

        items: list[DoubanItem] = []
        for index, raw in enumerate(raw_items):
            item = _search_item(raw, kind, start + index + 1)
            if item is not None:
                items.append(item)
        result = DoubanSearch(
            q=query,
            kind=kind,
            items=tuple(items),
            total=total,
            start=start,
            count=count,
            fetched_at=to_iso(utc_now()),
            has_more=bool(raw_items) and start + count < total,
        )
        if self.cache_sec:
            self._search_cache[key] = (now, result)
        return result

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
        if kind not in DOUBAN_KINDS:
            raise ValueError(f"未知豆瓣类型: {kind}")
        if sort not in DOUBAN_SORTS:
            raise ValueError(f"未知豆瓣排序: {sort}")
        start = max(0, int(start))
        count = min(100, max(1, int(count)))
        # Only whitelisted filter labels reach Douban; anything else is
        # treated as "no filter" instead of being forwarded blindly.
        selected_year = year if year in year_options() else ""
        selected_genre = genre if genre in genre_options(kind) else ""
        if selected_genre == ALL_LABEL:
            selected_genre = ""
        # Variety shows live in Douban's TV catalogue, so the type tag keeps
        # genre "喜剧" from matching ordinary TV comedies.
        tag_parts: list[str] = ["综艺"] if kind == "variety" else []
        if selected_genre:
            tag_parts.append(selected_genre)
        if selected_year:
            tag_parts.append(selected_year)
        tags = ",".join(tag_parts)
        key = (kind, sort, start, count, tags)
        now = time.monotonic()
        if not refresh and self.cache_sec:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_sec:
                return replace(cached[1], cached=True)

        try:
            raw_items, total_hint = self._recommend_items(
                "tv" if kind == "variety" else kind,
                sort,
                start,
                count,
                tags,
            )
            payload = {"total": total_hint}
        except DoubanError:
            # Douban sometimes answers with an HTML/anti-bot page; prefer the
            # last good chart over an empty page, same as a network failure.
            cached = self._cache.get(key)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            cached = self._cache.get(key)
            if cached:
                return replace(cached[1], cached=True, stale=True)
            raise DoubanError(f"豆瓣榜单读取失败: {exc}") from exc

        items: list[DoubanItem] = []
        expected_type = "movie" if kind == "movie" else "tv"
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            # The recommend feed mixes in chart collections (type "chart")
            # and occasionally cross-type subjects; keep only the kind that
            # was asked for.
            item_type = _text(raw.get("type"), 20)
            if item_type and item_type != expected_type:
                continue
            item_id = _digits(raw.get("id"), 20)
            title = _text(raw.get("title"), 200)
            if not item_id or not title:
                continue
            rating = raw.get("rating") if isinstance(raw.get("rating"), dict) else {}
            items.append(
                DoubanItem(
                    id=item_id,
                    kind="tv" if kind == "variety" else kind,
                    title=title,
                    year=_year(raw.get("year")),
                    rating=_float(rating.get("value")),
                    rating_count=_int(rating.get("count")),
                    subtitle=_text(raw.get("card_subtitle"), 500),
                    episodes_info=_text(raw.get("episodes_info"), 100),
                    cover_url=_cover(raw.get("pic")),
                    url=f"https://movie.douban.com/subject/{item_id}/",
                    rank=start + index + 1,
                )
            )

        # The recommend feed reports a total (capped at ~500).  Use the server
        # total rather than the kept-item count because filtered pages may have
        # dropped chart entries.  An empty page always ends pagination, even
        # when Douban's nominal total says otherwise.
        total = _int(payload.get("total"), 0)
        if not raw_items:
            has_more = False
        elif total:
            has_more = start + count < total
        else:
            has_more = len(raw_items) >= count

        chart = DoubanChart(
            kind=kind,
            sort=sort,
            items=tuple(items),
            total=total,
            fetched_at=to_iso(utc_now()),
            has_more=has_more,
        )
        if self.cache_sec:
            self._cache[key] = (now, chart)
        return chart

    def scan(
        self,
        kind: str,
        sort: str,
        *,
        year: str = "",
        genre: str = "",
        limit: int = 500,
        refresh: bool = False,
    ) -> DoubanChart:
        """Read a filtered chart from the start, up to Douban's ~500 cap.

        Keyword filtering used to look at one fetched page only.  When chart
        filters (year/genre) are active, scan the whole filtered chart so a
        keyword match on a later page is still found.  Every page goes through
        ``fetch`` and therefore shares its cache.
        """
        if kind not in DOUBAN_KINDS:
            raise ValueError(f"未知豆瓣类型: {kind}")
        if sort not in DOUBAN_SORTS:
            raise ValueError(f"未知豆瓣排序: {sort}")
        limit = min(500, max(1, int(limit)))
        items: list[DoubanItem] = []
        seen: set[str] = set()
        total = 0
        cached = True
        stale = False
        fetched_at = ""
        start = 0
        while start < limit:
            step = min(50, limit - start)
            page = self.fetch(
                kind,
                sort,
                start=start,
                count=step,
                year=year,
                genre=genre,
                refresh=refresh,
            )
            total = page.total or total
            cached = cached and page.cached
            stale = stale or page.stale
            fetched_at = page.fetched_at
            for item in page.items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)
            if not page.items or not page.has_more:
                break
            start += step
        return DoubanChart(
            kind=kind,
            sort=sort,
            items=tuple(items[:limit]),
            total=total or len(items),
            fetched_at=fetched_at or to_iso(utc_now()),
            cached=cached,
            stale=stale,
            has_more=False,
        )
