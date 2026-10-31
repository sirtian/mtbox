"""Minimal qBittorrent Web API client (v4.1+ / v5.x compatible)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..errors import QBError


@dataclass(frozen=True, slots=True)
class QBTorrent:
    hash: str
    name: str
    state: str
    progress: float
    size: int
    downloaded: int
    save_path: str
    content_path: str
    ratio: float
    seeding_time: int
    added_on: int
    completion_on: int
    category: str
    tags: str


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class QBittorrentClient:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(
            base_url=settings.qb_url.rstrip("/"),
            timeout=settings.qb_timeout_sec,
            follow_redirects=True,
            trust_env=settings.trust_env_proxy,
        )
        self._logged_in = False

    def set_password(self, password: str) -> None:
        """Replace the password and force a fresh login on the next request."""
        self.settings.qb_password = password
        self._client.cookies.clear()
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QBittorrentClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------- auth ----------

    def login(self) -> None:
        if not self.settings.qb_username:
            raise QBError("未配置 QB_USERNAME")
        try:
            response = self._client.post(
                "/api/v2/auth/login",
                data={
                    "username": self.settings.qb_username,
                    "password": self.settings.qb_password,
                },
                headers={"Referer": self.settings.qb_url.rstrip("/")},
            )
        except httpx.HTTPError as exc:
            raise QBError(f"qBittorrent 连接失败: {exc}") from exc
        if response.status_code == 403:
            raise QBError("qBittorrent 拒绝登录（IP 可能被封禁或 CSRF 校验失败）")
        if response.status_code >= 400:
            raise QBError(f"qBittorrent 登录失败: HTTP {response.status_code}")
        if "ok" not in (response.text or "").strip().lower():
            raise QBError("qBittorrent 登录失败：用户名或密码错误")
        self._logged_in = True

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._ensure_login()
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise QBError(f"qBittorrent 请求失败: {exc}") from exc
        if response.status_code == 403:
            # Session cookie expired: log in once and retry.
            self.login()
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise QBError(f"qBittorrent 请求失败: {exc}") from exc
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._send(method, path, **kwargs)
        if response.status_code >= 400:
            detail = (response.text or "")[:200].strip()
            raise QBError(f"qBittorrent {path} 失败: HTTP {response.status_code} {detail}".strip())
        return response

    # ---------- categories ----------

    def ensure_category(self, name: str) -> None:
        if not name:
            return
        categories = self.categories()
        if name in categories:
            return
        self._request(
            "POST",
            "/api/v2/torrents/createCategory",
            data={"category": name, "savePath": ""},
        )

    def categories(self) -> dict[str, dict[str, Any]]:
        response = self._request("GET", "/api/v2/torrents/categories")
        try:
            payload = response.json()
        except ValueError as exc:
            raise QBError("qBittorrent 分类列表返回非 JSON") from exc
        if not isinstance(payload, dict):
            raise QBError("qBittorrent 分类列表结构异常")
        return payload

    # ---------- torrents ----------

    def add(
        self,
        torrent: bytes,
        *,
        save_path: str,
        category: str = "",
        tags: tuple[str, ...] = (),
        paused: bool = False,
        seeding_time_limit_min: int | None = None,
        ratio_limit: float | None = None,
    ) -> None:
        if not torrent:
            raise QBError("种子内容为空")
        payload: dict[str, str] = {
            "urls": "",
            "savepath": save_path,
            "autoTMM": "false",
        }
        if category:
            payload["category"] = category
        if tags:
            payload["tags"] = ",".join(tags)
        if paused:
            payload["paused"] = "true"
        if seeding_time_limit_min is not None:
            payload["seedingTimeLimit"] = str(seeding_time_limit_min)
        if ratio_limit is not None:
            payload["ratioLimit"] = str(ratio_limit)

        files = {"torrents": ("mtbox.torrent", torrent, "application/x-bittorrent")}

        response = self._send("POST", "/api/v2/torrents/add", data=payload, files=files)
        if response.status_code >= 400 and paused:
            # qBittorrent 5.x replaced "paused" with "stopped".
            payload.pop("paused", None)
            payload["stopped"] = "true"
            files = {"torrents": ("mtbox.torrent", torrent, "application/x-bittorrent")}
            response = self._send("POST", "/api/v2/torrents/add", data=payload, files=files)

        if response.status_code >= 400:
            detail = (response.text or "")[:200].strip()
            raise QBError(f"qBittorrent 添加任务失败: HTTP {response.status_code} {detail}".strip())
        if "ok" not in (response.text or "").strip().lower():
            raise QBError(f"qBittorrent 添加任务失败: {(response.text or '').strip()[:200]}")

    def info(self, hashes: list[str]) -> list[QBTorrent]:
        cleaned = [item.strip().lower() for item in hashes if item.strip()]
        if not cleaned:
            return []
        response = self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": "|".join(cleaned)},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise QBError("qBittorrent 任务信息返回非 JSON") from exc
        if not isinstance(payload, list):
            raise QBError("qBittorrent 任务信息结构异常")
        return [self._parse_torrent(item) for item in payload if isinstance(item, dict)]

    def delete(self, hashes: list[str], *, delete_files: bool) -> None:
        cleaned = [item.strip().lower() for item in hashes if item.strip()]
        if not cleaned:
            return
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": "|".join(cleaned),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    def version(self) -> str:
        return (self._request("GET", "/api/v2/app/version").text or "").strip()

    # ---------- internals ----------

    @staticmethod
    def _parse_torrent(item: dict[str, Any]) -> QBTorrent:
        return QBTorrent(
            hash=str(item.get("hash", "")).lower(),
            name=str(item.get("name", "")),
            state=str(item.get("state", "")),
            progress=_as_float(item.get("progress")),
            size=_as_int(item.get("size")),
            downloaded=_as_int(item.get("downloaded")),
            save_path=str(item.get("save_path", "")),
            content_path=str(item.get("content_path", "")),
            ratio=_as_float(item.get("ratio")),
            seeding_time=_as_int(item.get("seeding_time")),
            added_on=_as_int(item.get("added_on")),
            completion_on=_as_int(item.get("completion_on")),
            category=str(item.get("category", "")),
            tags=str(item.get("tags", "")),
        )
