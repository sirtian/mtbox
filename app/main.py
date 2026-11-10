"""FastAPI application factory, lifespan and health endpoint."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .clients.douban import DoubanClient
from .clients.mteam import MTeamClient
from .clients.qbittorrent import QBittorrentClient
from .config import Settings, get_settings
from .db import Database
from .redaction import RedactFilter, register_secrets
from .services import credentials, selfcheck, settings_store
from .services.audit import log_event
from .services.downloader import DownloadService
from .services.notifier import Notifier
from .services.organizer import Organizer
from .services.poller import Poller
from .services.profile_guard import ProfileGuard
from .services.ratelimit import ApiLimiter
from .services.scheduler import Scheduler
from .services.search_service import SearchService
from .services.seeding import SeedingService
from .services.settings_store import get_value
from .services.subscriptions import SubscriptionService

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

log = logging.getLogger("mtbox")


def configure_logging(settings: Settings) -> None:
    register_secrets(settings.mt_api_key, settings.qb_password, settings.session_secret)
    root = logging.getLogger()
    existing = [handler for handler in root.handlers if getattr(handler, "_mtbox", False)]
    if existing:
        if all(getattr(handler, "_mtbox_path", None) == str(settings.log_dir) for handler in existing):
            return
        for handler in existing:
            root.removeHandler(handler)
            handler.close()

    settings.ensure_dirs()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactFilter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    stream._mtbox = True  # type: ignore[attr-defined]
    stream._mtbox_path = str(settings.log_dir)  # type: ignore[attr-defined]
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "mtbox.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    file_handler._mtbox = True  # type: ignore[attr-defined]
    file_handler._mtbox_path = str(settings.log_dir)  # type: ignore[attr-defined]
    root.addHandler(file_handler)


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    run_remote_checks: bool = True,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.ensure_dirs()
    configure_logging(app_settings)
    # Values from .env, kept as the fallback when a stored override is cleared.
    base_credentials = credentials.env_values(app_settings)

    db = database or Database(app_settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.init()
        settings_store.ensure_defaults(db)
        # Credentials saved at /settings take precedence over .env; register
        # the effective values so newly set secrets are also redacted.
        register_secrets(*credentials.apply_overrides(app_settings, db).values())

        limiter = ApiLimiter(app_settings, db)
        mt = MTeamClient(app_settings, limiter)
        qb = QBittorrentClient(app_settings)
        douban = DoubanClient(
            timeout_sec=app_settings.douban_timeout_sec,
            cache_sec=app_settings.douban_cache_sec,
            trust_env=app_settings.trust_env_proxy,
        )

        check_lock = threading.Lock()
        notifier = Notifier(db)
        subscriptions = SubscriptionService(db)
        # 修复历史数据：目标集数已全部整理完成、但状态仍停在 active 的订阅。
        subscriptions.finish_completed()
        downloader = DownloadService(app_settings, db, mt, qb, notifier=notifier)
        poller = Poller(db, qb)
        organizer = Organizer(app_settings, db, notifier=notifier, qb=qb)
        search = SearchService(
            app_settings, db, mt, downloader, notifier=notifier, douban=douban
        )
        guard = ProfileGuard(app_settings, db, mt, notifier=notifier)
        seeding = SeedingService(app_settings, db, qb, notifier=notifier)
        job_runner = Scheduler(
            app_settings,
            db,
            search,
            poller,
            organizer,
            guard=guard,
            seeding=seeding,
            notifier=notifier,
            check_lock=check_lock,
        )

        app.state.settings = app_settings
        app.state.base_credentials = base_credentials
        app.state.db = db
        app.state.limiter = limiter
        app.state.mt = mt
        app.state.qb = qb
        app.state.douban = douban
        app.state.notifier = notifier
        app.state.subscriptions = subscriptions
        app.state.downloader = downloader
        app.state.poller = poller
        app.state.organizer = organizer
        app.state.search = search
        app.state.guard = guard
        app.state.seeding = seeding
        app.state.check_lock = check_lock
        app.state.job_runner = job_runner
        app.state.scheduler = None

        # Missing credentials are warnings here: the user can save them from
        # /settings after the app is up. Directory/template errors still abort.
        local_checks = selfcheck.run_checks(
            app_settings, db, remote=False, credentials_required=False
        )
        selfcheck.require_valid(local_checks)

        checks = local_checks
        if run_remote_checks:
            checks += selfcheck.run_remote_checks(app_settings, db, mt=mt, qb=qb)
            # Re-evaluate the guard from the snapshot the self-check just wrote
            # (no extra profile call); stale snapshots are ignored.
            guard.sync_snapshot()
        app.state.checks = checks
        for check in checks:
            logger = log.warning if check.level in {"warn", "error"} else log.info
            logger("[startup] %s: %s", check.name, check.message)
        log_event(
            db,
            "WARN" if selfcheck.has_errors(checks) else "INFO",
            "startup",
            "应用启动",
            {"checks": [{"name": c.name, "level": c.level, "message": c.message} for c in checks]},
        )

        app.state.scheduler = job_runner.start()

        try:
            yield
        finally:
            job_runner.shutdown()
            mt.close()
            qb.close()
            douban.close()
            log.info("应用已停止")

    # No public JSON API: keep the OpenAPI/docs endpoints closed so an exposed
    # instance does not advertise its internal routes.
    app = FastAPI(
        title="mtbox",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.resolve_session_secret(),
        session_cookie="mtbox_session",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.state.templates = templates

    from .web.routes import install as install_web

    install_web(app)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        state = request.app.state
        database: Database = state.db
        try:
            with database.read() as conn:
                conn.execute("SELECT 1").fetchone()
            db_status = "ok"
        except Exception:  # pragma: no cover - defensive
            db_status = "error"

        qb_payload: dict[str, object] = {"ok": False}
        try:
            # Do not block the event loop (or the Docker healthcheck) while qB
            # is unreachable; 2s is plenty on a LAN.
            version = await asyncio.wait_for(
                asyncio.to_thread(state.qb.version), timeout=2.0
            )
            qb_payload = {"ok": True, "version": version}
        except Exception:
            pass

        usage = state.limiter.usage()
        scheduler = state.scheduler
        payload = {
            "ok": db_status == "ok",
            "db": db_status,
            "scheduler": {
                "running": bool(scheduler and scheduler.running),
                "jobs": len(scheduler.get_jobs()) if scheduler else 0,
            },
            "qb": qb_payload,
            "last_poll_at": get_value(database, "last_poll_at"),
            "budget": {"search_24h": usage.search, "torrent_24h": usage.torrent},
        }
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.port,
        workers=1,
        log_config=None,
    )


if __name__ == "__main__":
    main()
