"""Command-line entry points: check-config, search, backup, serve."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Settings, get_settings
from .db import Database
from .errors import MTBoxError
from .services import credentials, selfcheck, settings_store
from .services.ratelimit import ApiLimiter


def _prepare(settings: Settings) -> Database:
    settings.ensure_dirs()
    database = Database(settings.db_path)
    database.init()
    settings_store.ensure_defaults(database)
    # Credentials saved from the settings page override .env for CLI commands too.
    credentials.apply_overrides(settings, database)
    return database


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = _prepare(settings)
    checks = selfcheck.run_checks(settings, database, remote=False)

    if args.remote:
        limiter = ApiLimiter(settings, database)
        from .clients.mteam import MTeamClient
        from .clients.qbittorrent import QBittorrentClient

        mt = MTeamClient(settings, limiter)
        qb = QBittorrentClient(settings)
        try:
            checks += selfcheck.run_remote_checks(settings, database, mt=mt, qb=qb)
        finally:
            mt.close()
            qb.close()

    symbols = {"ok": "✓", "info": "·", "warn": "!", "error": "✗"}
    for check in checks:
        print(f"[{symbols.get(check.level, '?')}] {check.name}: {check.message}")

    errors = [check for check in checks if check.level == selfcheck.LEVEL_ERROR]
    if errors:
        print(f"\n配置校验失败：{len(errors)} 项错误", file=sys.stderr)
        return 1
    print("\n配置校验通过")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = _prepare(settings)
    limiter = ApiLimiter(settings, database)

    from .clients.mteam import MTeamClient

    with MTeamClient(settings, limiter) as client:
        result = client.search(
            args.keyword,
            mode=args.mode,
            page=args.page,
            page_size=min(args.limit, 100),
            wait=not args.no_wait,
        )

    if not result.items:
        print("没有匹配结果")
        return 0

    print(f"关键词 {result.query!r}：共 {result.total} 条，显示 {len(result.items)} 条")
    for item in result.items:
        flags = []
        if item.discount:
            flags.append(item.discount)
        if item.labels:
            flags.append("/".join(item.labels))
        suffix = f" [{' '.join(flags)}]" if flags else ""
        print(
            f"{item.id:>10}  {item.name}\n"
            f"            {_format_size(item.size_bytes)}  "
            f"做种 {item.seeders}  完成 {item.completed}{suffix}"
        )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = _prepare(settings)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(args.output) if args.output else settings.data_dir / "backups" / f"mtbox-{stamp}.db"
    database.vacuum_into(target)
    print(f"已备份到 {target}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    port = args.port if args.port is not None else settings.port
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=args.host,
        port=port,
        workers=1,
        log_config=None,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtbox", description="M-Team 订阅下载工具")
    parser.add_argument("--version", action="version", version=f"mtbox {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="校验配置、目录与模板")
    check.add_argument("--remote", action="store_true", help="同时测试 qBittorrent 与 M-Team")
    check.set_defaults(func=cmd_check_config)

    search = sub.add_parser("search", help="手动搜索 M-Team（受预算与间隔限制）")
    search.add_argument("keyword")
    search.add_argument("--mode", default="normal", help="normal / tvshow")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--no-wait", action="store_true", help="间隔不足时立即失败")
    search.set_defaults(func=cmd_search)

    backup = sub.add_parser("backup", help="备份 SQLite 数据库")
    backup.add_argument("--output", help="目标文件路径")
    backup.set_defaults(func=cmd_backup)

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=None, help="默认读取 PORT（8080）")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MTBoxError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
