"""M-Team API client (``x-api-key`` access token, no cookies, no signatures)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Settings
from ..errors import (
    MTeamApiError,
    MTeamAuthError,
    MTeamError,
    MTeamNetworkError,
    MTeamRateLimitError,
)
from ..services.ratelimit import ApiLimiter

MAX_TORRENT_BYTES = 32 * 1024 * 1024

# Documented by M-Team's OpenAPI schema for POST /torrent/search.
SORT_FIELDS = frozenset(
    {"CREATED_DATE", "SIZE", "SEEDERS", "LEECHERS", "TIMES_COMPLETED", "NAME"}
)
SORT_DIRECTIONS = frozenset({"ASC", "DESC"})


@dataclass(frozen=True, slots=True)
class MTeamTorrent:
    id: str
    name: str
    description: str
    category: str | None
    size_bytes: int
    file_count: int
    created_at: str | None
    labels: tuple[str, ...]
    imdb: str | None
    imdb_rating: float | None
    douban: str | None
    douban_rating: float | None
    seeders: int
    leechers: int
    completed: int
    discount: str | None
    discount_end_at: str | None
    visible: bool
    banned: bool
    # M-Team's ``smallDescr`` is the human-readable torrent title (for example
    # "示例剧 | 2026 | 第1季全12集 | 主演"), while ``name`` is the release/file
    # name.  Keep both so the UI can show what a result actually means.
    title: str = ""


@dataclass(frozen=True, slots=True)
class MTeamSearchResult:
    query: str
    items: tuple[MTeamTorrent, ...]
    total: int
    page: int
    page_size: int
    total_pages: int = 0


@dataclass(frozen=True, slots=True)
class MTeamProfile:
    ratio: float | None
    uploaded: int | None
    downloaded: int | None
    bonus: float | None
    raw: dict[str, Any]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("name", "id", "value"):
            if key in value:
                return str(value[key])
        return None
    text = str(value).strip()
    return text or None


class MTeamClient:
    def __init__(
        self,
        settings: Settings,
        limiter: ApiLimiter,
        *,
        client: httpx.Client | None = None,
        download_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self._client = client or httpx.Client(
            base_url=settings.mt_api_base,
            headers={
                "Accept": "application/json",
                "x-api-key": settings.mt_api_key,
                "User-Agent": settings.mt_user_agent,
            },
            timeout=settings.mt_timeout_sec,
            follow_redirects=False,
            trust_env=settings.trust_env_proxy,
        )
        self._download_client = download_client or httpx.Client(
            headers={"User-Agent": settings.mt_user_agent},
            timeout=settings.mt_timeout_sec,
            follow_redirects=True,
            trust_env=settings.trust_env_proxy,
        )

    def set_api_key(self, api_key: str) -> None:
        """Apply a rotated API key without rebuilding the HTTP client."""
        self.settings.mt_api_key = api_key
        self._client.headers["x-api-key"] = api_key

    def close(self) -> None:
        self._client.close()
        self._download_client.close()

    def __enter__(self) -> "MTeamClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------- endpoints ----------

    def search(
        self,
        keyword: str,
        *,
        mode: str = "normal",
        page: int = 1,
        page_size: int = 100,
        visible: int = 1,
        categories: list[str] | None = None,
        discount: str | None = None,
        sort_field: str | None = None,
        sort_direction: str | None = None,
        wait: bool = True,
    ) -> MTeamSearchResult:
        if page < 1:
            raise ValueError("page 必须 >= 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1..100")
        if sort_field is not None and sort_field not in SORT_FIELDS:
            raise ValueError(f"不支持的 sort_field: {sort_field}")
        if sort_direction is not None and sort_direction not in SORT_DIRECTIONS:
            raise ValueError(f"不支持的 sort_direction: {sort_direction}")

        body: dict[str, Any] = {
            "keyword": keyword,
            "mode": mode or "normal",
            "pageNumber": page,
            "pageSize": page_size,
        }
        if visible is not None:
            body["visible"] = visible
        if categories:
            body["categories"] = [str(item) for item in categories]
        if discount:
            body["discount"] = discount
        if sort_field:
            body["sortField"] = sort_field
            body["sortDirection"] = sort_direction or "DESC"

        self.limiter.acquire("search", wait=wait)
        try:
            data = self._post_json("/torrent/search", body)
        except MTeamRateLimitError:
            self.limiter.record("search", False)
            raise
        except MTeamError:
            self.limiter.record("search", False)
            raise
        else:
            self.limiter.record("search", True)

        items, total, page_number, returned_size, total_pages = self._normalize_search(
            data, page, page_size
        )
        return MTeamSearchResult(
            query=keyword,
            items=tuple(self._parse_item(item) for item in items if isinstance(item, dict)),
            total=total,
            page=page_number,
            page_size=returned_size,
            total_pages=total_pages,
        )

    def gen_download_url(self, mt_id: str) -> str:
        self.limiter.acquire("torrent")
        try:
            data = self._post_form("/torrent/genDlToken", {"id": str(mt_id)})
        except MTeamError:
            self.limiter.record("torrent", False)
            raise
        else:
            self.limiter.record("torrent", True)

        if not isinstance(data, str) or not data.strip():
            raise MTeamApiError("", "genDlToken 未返回下载地址")
        url = data.strip()
        if url.startswith("/"):
            url = urljoin(self.settings.mt_api_base + "/", url.lstrip("/"))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MTeamApiError("", "genDlToken 返回了非法地址")
        return url

    def download_torrent(self, url: str, *, max_bytes: int = MAX_TORRENT_BYTES) -> bytes:
        """Download the .torrent without the API key attached."""
        try:
            with self._download_client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise MTeamNetworkError(f"种子下载失败: HTTP {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MTeamApiError("", "种子文件超过大小限制")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise MTeamNetworkError(f"种子下载失败: {exc}") from exc
        return b"".join(chunks)

    def profile(self) -> MTeamProfile:
        self.limiter.acquire("profile")
        try:
            data = self._post_form("/member/profile", {})
        except MTeamError:
            self.limiter.record("profile", False)
            raise
        else:
            self.limiter.record("profile", True)

        raw = data if isinstance(data, dict) else {"data": data}
        # The live API nests stats under "memberCount"; older/other responses
        # may use "member" or put them at the top level.
        member: Any = None
        for key in ("memberCount", "member"):
            if isinstance(raw.get(key), dict):
                member = raw[key]
                break
        if member is None:
            member = raw
        ratio = _as_float(member.get("ratio", member.get("shareRate")))
        uploaded = member.get("uploaded", member.get("upload"))
        downloaded = member.get("downloaded", member.get("download"))
        bonus = member.get("bonus", member.get("score"))

        uploaded_int = _as_int(uploaded, -1) if uploaded is not None else None
        downloaded_int = _as_int(downloaded, -1) if downloaded is not None else None
        return MTeamProfile(
            ratio=ratio,
            uploaded=uploaded_int if uploaded_int is not None and uploaded_int >= 0 else None,
            downloaded=downloaded_int if downloaded_int is not None and downloaded_int >= 0 else None,
            bonus=_as_float(bonus),
            raw=raw,
        )

    # ---------- internals ----------

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        response = self._request("POST", path, json=payload)
        return self._unwrap(response)

    def _post_form(self, path: str, payload: dict[str, Any]) -> Any:
        response = self._request("POST", path, data=payload)
        return self._unwrap(response)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: MTeamError | None = None
        for attempt in range(3):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = MTeamNetworkError(f"请求超时: {exc}")
            except httpx.TransportError as exc:
                last_error = MTeamNetworkError(f"网络错误: {exc}")
            else:
                if response.status_code == 429 or self._looks_rate_limited(response):
                    raise MTeamRateLimitError("M-Team 限流或 Cloudflare 拦截")
                if response.status_code in (401, 403):
                    raise MTeamAuthError(f"M-Team 认证失败: HTTP {response.status_code}")
                if response.status_code >= 500:
                    last_error = MTeamNetworkError(f"M-Team 服务端错误: HTTP {response.status_code}")
                elif response.status_code >= 400:
                    raise MTeamError(f"M-Team 请求失败: HTTP {response.status_code}")
                else:
                    return response
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        raise last_error or MTeamNetworkError("M-Team 请求失败")

    @staticmethod
    def _looks_rate_limited(response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        body = (response.text or "").lower()
        return "1010" in body or "cloudflare" in body or "too many" in body

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MTeamApiError("", "M-Team 返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise MTeamApiError("", "M-Team 返回结构异常")
        code = str(payload.get("code", "")).strip()
        message = str(payload.get("message", "")).strip()
        success = code == "0" or (not code and message.upper() == "SUCCESS")
        if not success:
            lowered = message.lower()
            if any(token in lowered for token in ("token", "api key", "unauthorized", "permission")):
                raise MTeamAuthError(f"M-Team 认证失败: {message}")
            raise MTeamApiError(code, message)
        return payload.get("data")

    @staticmethod
    def _normalize_search(
        data: Any, requested_page: int, requested_size: int
    ) -> tuple[list[Any], int, int, int, int]:
        if isinstance(data, dict):
            raw_items = data.get("data") or data.get("list") or data.get("torrents") or []
            total = _as_int(data.get("total") or data.get("totalCount"), len(raw_items))
            page = _as_int(data.get("pageNumber"), requested_page)
            page_size = _as_int(data.get("pageSize"), requested_size)
            total_pages = _as_int(data.get("totalPages"))
            if total_pages <= 0 and total > 0 and page_size > 0:
                total_pages = (total + page_size - 1) // page_size
        elif isinstance(data, list):
            raw_items = data
            total = len(data)
            page = requested_page
            page_size = requested_size
            total_pages = 1 if data else 0
        else:
            raise MTeamApiError("", "搜索结果结构异常")
        if not isinstance(raw_items, list):
            raise MTeamApiError("", "搜索结果列表异常")
        return raw_items, total, page, page_size, total_pages

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> MTeamTorrent:
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        raw_labels = item.get("labelsNew") or item.get("labels") or []
        labels = tuple(
            text
            for text in (
                _as_text(label) if not isinstance(label, dict) else _as_text(label.get("name"))
                for label in raw_labels
            )
            if text
        )
        title = str(
            item.get("smallDescr") or item.get("title") or item.get("description") or ""
        )[:500]
        return MTeamTorrent(
            id=str(item.get("id", "")),
            name=str(item.get("name") or item.get("title") or "")[:500],
            description=str(
                item.get("smallDescr") or item.get("description") or ""
            )[:500],
            title=title,
            category=_as_text(item.get("category")),
            size_bytes=_as_int(item.get("size")),
            file_count=_as_int(item.get("numfiles")),
            created_at=_as_text(item.get("createdDate") or item.get("publishDate")),
            labels=labels,
            imdb=_as_text(item.get("imdb")),
            imdb_rating=_as_float(item.get("imdbRating")),
            douban=_as_text(item.get("douban")),
            douban_rating=_as_float(item.get("doubanRating")),
            seeders=_as_int(status.get("seeders")),
            leechers=_as_int(status.get("leechers")),
            completed=_as_int(status.get("timesCompleted")),
            discount=_as_text(status.get("discount")),
            discount_end_at=_as_text(status.get("discountEndTime")),
            visible=bool(_as_int(status.get("visible"), 1)),
            banned=bool(_as_int(status.get("banned"), 0)),
        )
