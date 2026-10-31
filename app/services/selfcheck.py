"""Startup configuration validation and health checks."""

from __future__ import annotations

import os
import shutil
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..clients.mteam import MTeamClient
from ..clients.qbittorrent import QBittorrentClient
from ..config import Settings
from ..db import Database
from ..errors import MTBoxError
from ..timeutil import utc_now_iso
from . import settings_store

ALLOWED_PLACEHOLDERS = {
    "library",
    "category",
    "title",
    "original_title",
    "year",
    "season",
    "episode",
    "episode_end",
    "resolution",
    "video_codec",
    "ext",
    "group",
}

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    message: str

    @property
    def ok(self) -> bool:
        return self.level != LEVEL_ERROR


def has_errors(checks: Iterable[Check]) -> bool:
    return any(check.level == LEVEL_ERROR for check in checks)


def require_valid(checks: Iterable[Check]) -> None:
    errors = [check for check in checks if check.level == LEVEL_ERROR]
    if errors:
        from ..errors import ConfigError

        detail = "；".join(f"{check.name}: {check.message}" for check in errors)
        raise ConfigError(f"配置校验失败 - {detail}")


def _writable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"目录不存在: {path}"
    if not path.is_dir():
        return False, f"不是目录: {path}"
    if not os.access(path, os.W_OK):
        return False, f"目录不可写: {path}"
    return True, str(path)


def _template_fields(template: str) -> set[str] | None:
    try:
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(template)
            if field_name is not None
        }
    except ValueError:
        return None
    return fields


def run_checks(
    settings: Settings,
    db: Database,
    *,
    mt: MTeamClient | None = None,
    qb: QBittorrentClient | None = None,
    remote: bool = True,
    credentials_required: bool = True,
) -> list[Check]:
    checks: list[Check] = []

    has_api_key = bool(settings.mt_api_key.strip())
    # The web app can start without a key so the user can save one at
    # /settings; check-config still treats it as a fatal configuration error.
    key_level = LEVEL_OK if has_api_key else (
        LEVEL_ERROR if credentials_required else LEVEL_WARN
    )
    checks.append(
        Check(
            "MT API Key",
            key_level,
            "已配置" if has_api_key else "未配置 MT API Key（可在设置页填写）",
        )
    )

    for label, path in (
        ("下载目录", settings.download_dir),
        ("媒体库目录", settings.library_dir),
        ("数据目录", settings.data_dir),
    ):
        ok, message = _writable(path)
        checks.append(Check(label, LEVEL_OK if ok else LEVEL_ERROR, message))

    try:
        path_map = settings.path_map()
    except ValueError as exc:
        checks.append(Check("路径映射", LEVEL_ERROR, str(exc)))
        path_map = []
    else:
        checks.append(
            Check("路径映射", LEVEL_OK, ", ".join(f"{a} → {b}" for a, b in path_map))
        )

    if path_map:
        try:
            mapped = settings.to_local_path(settings.qb_save_path)
        except ValueError as exc:
            checks.append(Check("QB_SAVE_PATH 映射", LEVEL_ERROR, str(exc)))
        else:
            if mapped == settings.download_dir or settings.download_dir in mapped.parents:
                checks.append(Check("QB_SAVE_PATH 映射", LEVEL_OK, str(mapped)))
            else:
                checks.append(
                    Check(
                        "QB_SAVE_PATH 映射",
                        LEVEL_WARN,
                        f"映射结果 {mapped} 不在 DOWNLOAD_DIR {settings.download_dir} 下",
                    )
                )

    settings_store.ensure_defaults(db)
    for key in ("movie_path_template", "tv_path_template"):
        template = settings_store.get_value(db, key) or ""
        fields = _template_fields(template)
        label = f"命名模板 {key}"
        if fields is None:
            checks.append(Check(label, LEVEL_ERROR, f"{key} 格式非法: {template!r}"))
            continue
        unknown = fields - ALLOWED_PLACEHOLDERS
        if unknown:
            checks.append(
                Check(
                    label,
                    LEVEL_ERROR,
                    f"{key} 含未知占位符: {', '.join(sorted(unknown))}",
                )
            )
        elif "title" not in fields:
            checks.append(Check(label, LEVEL_ERROR, f"{key} 缺少 {{title}}"))
        else:
            checks.append(Check(label, LEVEL_OK, f"{key} 占位符合法"))

    checks.append(_check_same_device(settings))
    checks.append(_check_disk(settings, db))
    checks.append(_check_budget_plan(settings, db))

    category_map = settings_store.get_json(db, "category_map", {}) or {}
    if not category_map:
        checks.append(
            Check("分类/模式映射", LEVEL_INFO, "category_map 未校准，M1 实测后配置")
        )

    if remote:
        checks.extend(run_remote_checks(settings, db, mt=mt, qb=qb))

    return checks


def run_remote_checks(
    settings: Settings,
    db: Database,
    *,
    mt: MTeamClient | None = None,
    qb: QBittorrentClient | None = None,
) -> list[Check]:
    checks: list[Check] = []
    if qb is not None:
        checks.append(_check_qb(qb))
    if mt is not None:
        checks.append(_check_mt(settings, db, mt))
    return checks


def _check_same_device(settings: Settings) -> Check:
    try:
        download_dev = os.stat(settings.download_dir).st_dev
        library_dev = os.stat(settings.library_dir).st_dev
    except OSError as exc:
        return Check("硬链接前提", LEVEL_WARN, f"无法比较设备号: {exc}")
    if download_dev != library_dev:
        return Check("硬链接前提", LEVEL_WARN, "下载目录与媒体库不在同一存储池，硬链接不可用")
    return Check("硬链接前提", LEVEL_OK, "同一存储池")


def _check_disk(settings: Settings, db: Database) -> Check:
    try:
        usage = shutil.disk_usage(settings.library_dir)
    except OSError as exc:
        return Check("磁盘水位", LEVEL_WARN, f"无法读取磁盘用量: {exc}")
    percent = usage.used / usage.total * 100 if usage.total else 0.0
    threshold = settings_store.get_int(db, "disk_pause_percent", 90)
    if percent >= threshold:
        return Check(
            "磁盘水位",
            LEVEL_WARN,
            f"媒体库所在盘已用 {percent:.1f}%（阈值 {threshold}%），将暂停抓取",
        )
    return Check("磁盘水位", LEVEL_OK, f"已用 {percent:.1f}%")


def _check_budget_plan(settings: Settings, db: Database) -> Check:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT interval_min FROM subscription "
            "WHERE status = 'active' AND deleted_at IS NULL"
        ).fetchall()
    theoretical = 0.0
    for row in rows:
        interval = max(1, int(row["interval_min"] or 30))
        theoretical += 24 * 60 / interval
    budget = settings_store.effective_search_budget(db, settings.mt_search_daily_budget)
    if theoretical > budget * 0.8:
        return Check(
            "搜索预算规划",
            LEVEL_WARN,
            f"订阅理论调用 {theoretical:.0f} 次/天，超过预算 {budget} 的 80%，建议调大间隔",
        )
    return Check("搜索预算规划", LEVEL_OK, f"理论 {theoretical:.0f} 次/天，预算 {budget}")


def _check_qb(qb: QBittorrentClient) -> Check:
    try:
        version = qb.version()
    except MTBoxError as exc:
        return Check("qBittorrent", LEVEL_WARN, f"不可用: {exc}")
    return Check("qBittorrent", LEVEL_OK, version or "已连接")


def _check_mt(settings: Settings, db: Database, mt: MTeamClient) -> Check:
    try:
        profile = mt.profile()
    except MTBoxError as exc:
        return Check("M-Team", LEVEL_WARN, f"不可用: {exc}")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO account_snapshot(ratio, uploaded, downloaded, bonus, captured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (profile.ratio, profile.uploaded, profile.downloaded, profile.bonus, utc_now_iso()),
        )
    if profile.ratio is None:
        return Check("M-Team", LEVEL_WARN, "已连接，但未解析到分享率")
    return Check("M-Team", LEVEL_OK, f"分享率 {profile.ratio:.2f}")
