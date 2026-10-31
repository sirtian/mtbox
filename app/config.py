from __future__ import annotations

import json
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class Settings(BaseSettings):
    """Environment-backed configuration.

    MT API Key and qB password may optionally be overridden by values saved from
    the settings page; see ``app.services.credentials``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # M-Team
    mt_api_key: str = ""
    mt_api_base: str = "https://api.m-team.cc/api"
    mt_user_agent: str = DEFAULT_USER_AGENT
    mt_search_min_interval_sec: int = 20
    mt_search_daily_budget: int = 400
    mt_download_daily_budget: int = 60
    mt_profile_daily_budget: int = 200
    mt_timeout_sec: float = 30.0

    # Douban charts (public pages; no account or cookie)
    douban_timeout_sec: float = 15.0
    douban_cache_sec: int = 600

    # Outbound HTTP. Off by default: the target NAS reaches MT/Douban directly,
    # and a stale system proxy would otherwise break TLS to M-Team.
    trust_env_proxy: bool = False

    # qBittorrent
    qb_url: str = "http://127.0.0.1:8080"
    qb_username: str = ""
    qb_password: str = ""
    qb_save_path: str = "/downloads"
    qb_category: str = "mtbox"
    qb_tags: str = "mtbox"
    qb_timeout_sec: float = 15.0

    # Paths (container namespace)
    download_dir: Path = Path("/downloads")
    library_dir: Path = Path("/media")
    data_dir: Path = Path("/config")
    path_map_json: str = '[["/downloads","/downloads"]]'

    # Admin
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""

    # Server
    port: int = 8080

    # Behaviour
    dry_run: bool = True
    scheduler_enabled: bool = True
    organize_delay_sec: int = 15
    min_video_size_mb: int = 50
    log_level: str = "INFO"
    tz: str = "Asia/Shanghai"

    # ---------- derived ----------

    @field_validator("mt_api_base")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("PORT 必须在 1-65535 之间")
        return value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mtbox.db"

    @property
    def torrents_dir(self) -> Path:
        return self.data_dir / "torrents"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def path_map(self) -> list[tuple[str, str]]:
        """Return (qb_prefix, local_prefix) pairs, longest prefix first."""
        try:
            raw = json.loads(self.path_map_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"PATH_MAP_JSON 不是合法 JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("PATH_MAP_JSON 必须是数组")
        pairs: list[tuple[str, str]] = []
        for entry in raw:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) != 2
                or not all(
                    isinstance(part, str)
                    and (part.startswith("/") or Path(part).is_absolute())
                    for part in entry
                )
            ):
                raise ValueError(f"PATH_MAP_JSON 条目非法: {entry!r}")
            pairs.append((entry[0].rstrip("/") or "/", entry[1].rstrip("/") or "/"))
        if not pairs:
            raise ValueError("PATH_MAP_JSON 不能为空")
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def to_local_path(self, qb_path: str) -> Path:
        """Translate a path reported by qBittorrent into this container's namespace."""
        normalized = "/" + qb_path.replace("\\", "/").lstrip("/")
        for qb_prefix, local_prefix in self.path_map():
            if normalized == qb_prefix or normalized.startswith(qb_prefix + "/"):
                suffix = normalized[len(qb_prefix) :]
                return Path(local_prefix + suffix)
        raise ValueError(f"路径 {qb_path!r} 不在 PATH_MAP_JSON 中，无法映射")

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.torrents_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_session_secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        self.ensure_dirs()
        secret_file = self.data_dir / "secret.key"
        if secret_file.exists():
            value = secret_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_hex(32)
        secret_file.write_text(value, encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
