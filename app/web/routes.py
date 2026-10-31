"""Server-rendered UI: login, search, subscriptions and task control."""

from __future__ import annotations

import json
import logging
import string
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..clients.douban import (
    ALL_LABEL,
    DOUBAN_KINDS,
    DOUBAN_SORTS,
    genre_options,
    year_options,
)
from ..clients.release_name import parse_release, season_from_title
from ..db import Database
from ..errors import Conflict, DoubanError, MTBoxError, NotFound, RateLimitBusy
from ..redaction import register_secrets
from ..security import (
    LoginThrottle,
    admin_password_configured,
    get_csrf_token,
    verify_admin_password,
    verify_csrf,
)
from ..services import credentials, profile_guard, settings_store
from ..services.audit import log_event, recent_events
from ..services.downloader import DownloadService
from ..services.matcher import Matcher, is_free
from ..services.mteam_categories import (
    CATEGORY_GROUPS,
    CATEGORY_INDEX,
    DISCOUNT_OPTIONS,
    PAGE_SIZES,
    SORT_OPTIONS,
    category_label,
    library_category_for,
    mode_for_filter,
    normalize_discount,
    normalize_page_size,
    normalize_sort,
    resolve_category,
    sort_label,
    sort_params,
)
from ..services.notifier import notify_config_error
from ..services.organizer import Organizer
from ..services.poller import Poller
from ..services.search_service import candidate_payload
from ..services.selfcheck import ALLOWED_PLACEHOLDERS
from ..services.subscriptions import SubscriptionService
from ..timeutil import hours_ago_iso, local_display, parse_dt, to_iso, utc_now

log = logging.getLogger("mtbox.web")
router = APIRouter()

DOUBAN_PAGE_SIZE = 20
DOUBAN_PAGE_SIZES = (20, 50, 100)
DOUBAN_MAX_START = 5000

# 豆瓣“类别”与媒体库分类命名不完全一致：只对确定不同的别名做映射，
# 其余类别原样带到订阅页的“媒体库分类”，用户仍可修改。
GENRE_CATEGORY_ALIASES = {"动画": "动漫"}


class LoginRequired(Exception):
    pass


# ---------- helpers ----------


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def _flash(request: Request, message: str, level: str = "info") -> None:
    items = list(request.session.get("_flash") or [])
    items.append({"level": level, "message": message})
    request.session["_flash"] = items[-10:]


def _ctx(request: Request, **extra: object) -> dict[str, object]:
    settings = request.app.state.settings
    context: dict[str, object] = {
        "version": __version__,
        "dry_run": settings.dry_run,
        "user": request.session.get("user"),
        "csrf_token": get_csrf_token(request),
        "flash": request.session.pop("_flash", []),
        "current_path": request.url.path,
    }
    context.update(extra)
    return context


def _render(
    request: Request, name: str, *, status_code: int = 200, **extra: object
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, name, _ctx(request, **extra), status_code=status_code
    )


def _require_user(request: Request) -> str:
    settings = request.app.state.settings
    user = request.session.get("user")
    if not user or user != settings.admin_username:
        raise LoginRequired()
    return str(user)


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _check_csrf(request: Request, token: str | None) -> Response | None:
    if not verify_csrf(request, token):
        return HTMLResponse("<h1>CSRF 校验失败</h1><p>请返回页面重新提交。</p>", status_code=400)
    return None


def state_db(request: Request):
    return request.app.state.db


def _services(request: Request) -> tuple[SubscriptionService, DownloadService, Poller, Organizer]:
    state = request.app.state
    return state.subscriptions, state.downloader, state.poller, state.organizer


# ---------- auth ----------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    settings = request.app.state.settings
    if request.session.get("user") == settings.admin_username:
        return RedirectResponse("/", status_code=303)
    return _render(
        request,
        "login.html",
        next=_safe_next(request.query_params.get("next")),
        configured=admin_password_configured(settings),
        error=None,
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
) -> Response:
    settings = request.app.state.settings
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed

    throttle: LoginThrottle = request.app.state.login_throttle
    ip = request.client.host if request.client else "unknown"
    locked = throttle.locked_for(ip)
    if locked:
        return _render(
            request,
            "login.html",
            next=_safe_next(next),
            configured=True,
            error=f"失败次数过多，请 {locked} 秒后再试",
        )

    configured = admin_password_configured(settings)
    if (
        configured
        and username == settings.admin_username
        and verify_admin_password(settings, password)
    ):
        throttle.reset(ip)
        request.session.clear()
        request.session["user"] = settings.admin_username
        log_event(state_db(request), "INFO", "auth", "登录成功", {"ip": ip})
        return RedirectResponse(_safe_next(next), status_code=303)

    locked_for = throttle.record_failure(ip)
    log_event(state_db(request), "WARN", "auth", "登录失败", {"ip": ip, "username": username})
    message = "用户名或密码错误"
    if locked_for:
        message = f"失败次数过多，已锁定 {locked_for} 秒"
    if not configured:
        message = "尚未配置 ADMIN_PASSWORD"
    return _render(
        request,
        "login.html",
        next=_safe_next(next),
        configured=configured,
        error=message,
    )


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- dashboard ----------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Response:
    _require_user(request)
    state = request.app.state
    database = state.db
    with database.read() as conn:
        counts = {
            "subscriptions": conn.execute(
                "SELECT COUNT(*) AS n FROM subscription WHERE deleted_at IS NULL"
            ).fetchone()["n"],
            "torrents": conn.execute(
                "SELECT COUNT(*) AS n FROM torrent t "
                "JOIN subscription s ON s.id = t.subscription_id "
                "WHERE t.state NOT IN ('deleted') AND s.deleted_at IS NULL"
            ).fetchone()["n"],
            "candidates": conn.execute(
                "SELECT COUNT(*) AS n FROM torrent t "
                "JOIN subscription s ON s.id = t.subscription_id "
                "WHERE t.state = 'candidate' AND s.deleted_at IS NULL"
            ).fetchone()["n"],
            "downloading": conn.execute(
                "SELECT COUNT(*) AS n FROM torrent t "
                "JOIN subscription s ON s.id = t.subscription_id "
                "WHERE t.state IN ('grabbed','downloading') AND s.deleted_at IS NULL"
            ).fetchone()["n"],
            "to_organize": conn.execute(
                "SELECT COUNT(*) AS n FROM torrent t "
                "JOIN subscription s ON s.id = t.subscription_id "
                "WHERE t.state IN ('completed','organize_failed') "
                "AND s.deleted_at IS NULL"
            ).fetchone()["n"],
            "events": conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"],
            "errors_24h": conn.execute(
                "SELECT COUNT(*) AS n FROM event_log WHERE level = 'ERROR' AND created_at >= ?",
                (hours_ago_iso(24),),
            ).fetchone()["n"],
        }
        snapshot = conn.execute(
            "SELECT * FROM account_snapshot ORDER BY id DESC LIMIT 1"
        ).fetchone()
    usage = state.limiter.usage()
    guard = profile_guard.load(database)
    guard_labels = {"ok": "正常", "free_only": "仅免费", "paused": "已暂停"}
    return _render(
        request,
        "dashboard.html",
        checks=state.checks,
        usage=usage.as_dict(),
        budgets=state.limiter.budgets(),
        counts=counts,
        snapshot=dict(snapshot) if snapshot else None,
        events=recent_events(database, limit=12),
        errors=recent_events(database, limit=5, level="ERROR"),
        jobs=state.job_runner.status(),
        scheduler_running=state.scheduler is not None,
        guard=guard,
        guard_label=guard_labels.get(guard.mode, guard.mode),
        disk=state.seeding.disk_usage(),
    )


# ---------- douban charts ----------


@router.get("/douban", response_class=HTMLResponse)
def douban_page(request: Request) -> Response:
    _require_user(request)
    state = request.app.state
    # 记住上次选择的筛选条件：点击“订阅”跳走后，再从导航回到 /douban 时
    # 仍按上次的剧集/电影/排序/年份/类别/每页展示。带筛选参数的请求以
    # 参数为准（分页键 start/page 不算筛选，沿用记忆值）。
    params = request.query_params
    session = request.session
    has_filters = any(
        key in params for key in ("kind", "sort", "year", "genre", "size")
    )
    saved = session.get("douban_filters")
    source = params if has_filters else (saved if isinstance(saved, dict) else {})
    kind = str(source.get("kind") or "tv")
    if kind not in DOUBAN_KINDS:
        kind = "tv"
    sort = str(source.get("sort") or "U")
    if sort not in DOUBAN_SORTS:
        sort = "U"
    refresh = params.get("refresh") == "1"
    try:
        size = int(source.get("size") or DOUBAN_PAGE_SIZE)
    except ValueError:
        size = DOUBAN_PAGE_SIZE
    if size not in DOUBAN_PAGE_SIZES:
        size = DOUBAN_PAGE_SIZE
    # The jump box submits a page number; prev/next links keep using start.
    raw_page = request.query_params.get("page")
    if raw_page not in (None, ""):
        try:
            page_number = int(raw_page)
        except ValueError:
            page_number = 1
        start = (max(1, page_number) - 1) * size
    else:
        try:
            start = int(request.query_params.get("start") or 0)
        except ValueError:
            start = 0
    start = min(max(0, start), DOUBAN_MAX_START)
    years = year_options()
    year = str(source.get("year") or "").strip()
    if year not in years:
        year = ""
    if year == ALL_LABEL:
        year = ""
    genres = genre_options(kind)
    genre = str(source.get("genre") or "").strip()
    if genre not in genres:
        genre = ""
    if genre == ALL_LABEL:
        genre = ""
    filters = {"kind": kind, "sort": sort, "year": year, "genre": genre, "size": size}
    if has_filters and session.get("douban_filters") != filters:
        session["douban_filters"] = filters
    chart = None
    error: str | None = None
    items: list[dict[str, object]] = []
    try:
        douban = getattr(state, "douban", None)
        if douban is None:
            raise DoubanError("豆瓣客户端未初始化")
        chart = douban.fetch(
            kind,
            sort,
            start=start,
            count=size,
            year=year,
            genre=genre,
            refresh=refresh,
        )
    except DoubanError as exc:
        error = str(exc)
    else:
        # 榜单当前选中的“类别”直接作为订阅的“媒体库分类”预填，
        # 这样从榜单点“订阅”就不用再手填一次。
        library_category = GENRE_CATEGORY_ALIASES.get(genre, genre)
        for item in chart.items:
            prefill: dict[str, str] = {
                "kind": item.kind,
                "title": item.title,
                "douban_id": item.id,
            }
            if item.year:
                prefill["year"] = str(item.year)
            if item.kind == "tv":
                prefill["season"] = str(season_from_title(item.title) or 1)
            if library_category:
                prefill["library_category"] = library_category
            cover_url = (
                "/douban/cover?url=" + quote(item.cover_url, safe="")
                if item.cover_url
                else ""
            )
            items.append(
                {
                    "item": item,
                    "cover_url": cover_url,
                    "prefill_url": "/subscriptions/new?" + urlencode(prefill),
                }
            )
    page = start // size + 1
    has_prev = start > 0
    has_next = bool(chart and chart.has_more)
    total_pages = 0
    if chart and chart.total:
        total_pages = (chart.total + size - 1) // size

    def link(**overrides: object) -> str:
        params: dict[str, object] = {"kind": kind, "sort": sort}
        if size != DOUBAN_PAGE_SIZE:
            params["size"] = size
        if year:
            params["year"] = year
        if genre:
            params["genre"] = genre
        for key, value in overrides.items():
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = value
        return "/douban?" + urlencode(params)

    kind_links = [
        {
            "code": code,
            "label": label,
            "href": link(
                kind=code,
                genre=genre if not genre or genre in genre_options(code) else "",
            ),
        }
        for code, label in DOUBAN_KINDS.items()
    ]
    sort_links = [
        {"code": code, "label": label, "href": link(sort=code)}
        for code, label in DOUBAN_SORTS.items()
    ]
    return _render(
        request,
        "douban.html",
        kind=kind,
        sort=sort,
        kind_links=kind_links,
        sort_links=sort_links,
        year=year,
        genre=genre,
        year_options=years,
        genre_options=genres,
        all_label=ALL_LABEL,
        chart=chart,
        items=items,
        error=error,
        refresh=refresh,
        start=start,
        page=page,
        has_prev=has_prev,
        has_next=has_next,
        size=size,
        size_options=DOUBAN_PAGE_SIZES,
        total_pages=total_pages,
        refresh_url=link(refresh="1", start=start),
        clear_url=link(year="", genre=""),
        prev_url=link(start=max(0, start - size)),
        next_url=link(start=start + size),
        fetched_display=local_display(chart.fetched_at) if chart else "",
    )


@router.get("/douban/cover")
def douban_cover(request: Request, url: str = "") -> Response:
    """Serve a poster from Douban's CDN with the required Referer.

    Browsers cannot load doubanio.com images directly from a LAN origin
    (the CDN answers 418/403).  The client validates the URL against a CDN
    allowlist before fetching, so this stays a narrow image passthrough.
    """
    _require_user(request)
    douban = getattr(request.app.state, "douban", None)
    if douban is None:
        return Response(
            status_code=404, headers={"Cache-Control": "no-store"}
        )
    try:
        body, media_type = douban.fetch_cover(url)
    except DoubanError:
        return Response(
            status_code=404, headers={"Cache-Control": "no-store"}
        )
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------- search ----------


def _pending_torrent(
    *,
    mt_id: str,
    name: str,
    size_bytes: str | int,
    seeders: str | int,
    discount: str,
    category: str,
    publish_at: str,
) -> dict[str, object] | None:
    """Normalise the search result carried into the new-subscription form.

    Values arrive as query/form strings, so a crafted link could claim any
    ID or an oversized title.  Anything that does not look like a real M-Team
    torrent ID is dropped and the subscription is created without an immediate
    grab.
    """
    torrent_id = str(mt_id).strip()
    title = str(name).strip()[:500]
    if not torrent_id.isdigit() or len(torrent_id) > 20 or not title:
        return None

    def as_count(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    category_id = str(category).strip()
    if category_id not in CATEGORY_INDEX:
        category_id = ""
    return {
        "mt_id": torrent_id,
        "name": title,
        "size_bytes": as_count(size_bytes),
        "seeders": as_count(seeders),
        "discount": str(discount).strip()[:32],
        "category": category_id,
        "publish_at": str(publish_at).strip()[:40],
    }


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> Response:
    _require_user(request)
    subscriptions, _, _, _ = _services(request)
    params = request.query_params

    keyword = (params.get("keyword") or "").strip()[:100]
    category_key = (params.get("category") or "").strip()
    sub_id = (params.get("sub") or "").strip()
    # Allow a leaf ID in ``category`` as a shortcut for ``?category=movie&sub=419``.
    if category_key in CATEGORY_INDEX and not sub_id:
        sub_id, category_key = category_key, ""
    group, categories = resolve_category(category_key, sub_id)
    sub_selected = sub_id if sub_id in CATEGORY_INDEX else ""
    library_category = library_category_for(group, sub_selected)
    mode = mode_for_filter(group, params.get("mode"))

    sort_key = normalize_sort(params.get("sort"))
    sort_field, sort_direction = sort_params(sort_key)
    discount = normalize_discount(params.get("discount"))
    page_size = normalize_page_size(params.get("size"))
    try:
        page = max(1, min(1000, int(params.get("page") or 1)))
    except ValueError:
        page = 1
    browse = params.get("browse") == "1"
    has_query = bool(keyword or categories or discount or browse)

    def link(**overrides: object) -> str:
        values: dict[str, object] = {
            "keyword": keyword,
            "category": group.key if group else "",
            "sub": sub_selected,
            "sort": sort_key,
            "discount": discount,
            "size": page_size,
            "mode": mode if group is None and mode != "normal" else "",
            "browse": "1" if browse else "",
            "page": page,
        }
        values.update(overrides)
        cleaned = {key: value for key, value in values.items() if value not in ("", None)}
        return "/search?" + urlencode(cleaned)

    results: list[dict[str, object]] = []
    error: str | None = None
    retry_after: int | None = None
    total = 0
    total_pages = 0

    if has_query:
        state = request.app.state
        try:
            found = state.mt.search(
                keyword,
                mode=mode,
                page=page,
                page_size=page_size,
                categories=categories,
                discount=discount or None,
                sort_field=sort_field,
                sort_direction=sort_direction,
                wait=False,
            )
        except RateLimitBusy as exc:
            error = str(exc)
            retry_after = exc.retry_after
        except MTBoxError as exc:
            error = str(exc)
        else:
            total = found.total
            total_pages = found.total_pages
            if total_pages <= 0 and total > 0:
                total_pages = (total + page_size - 1) // page_size
            for item in found.items:
                parsed = parse_release(item.name)
                kind_guess = (
                    "tv"
                    if (parsed.episodes or parsed.is_batch or parsed.season is not None)
                    else "movie"
                )
                title_hint = parsed.title_hint or item.name
                prefill = {
                    "kind": kind_guess,
                    "title": title_hint,
                    "year": parsed.year or "",
                    "season": parsed.season or 1,
                    "douban_id": _digits(item.douban),
                    "imdb_id": item.imdb or "",
                    # The selected filter category becomes the media library
                    # category; the chosen result travels along so confirming
                    # the form can grab it without a manual candidate step.
                    "mt_id": item.id,
                    "mt_name": item.name,
                    "mt_size_bytes": item.size_bytes,
                    "mt_seeders": item.seeders,
                    "mt_discount": item.discount or "",
                    "mt_category": item.category or "",
                    "mt_publish_at": item.created_at or "",
                }
                if library_category:
                    prefill["library_category"] = library_category
                results.append(
                    {
                        "item": item,
                        "parsed": parsed,
                        "kind_guess": kind_guess,
                        "category_name": category_label(item.category),
                        "size_gb": round(item.size_bytes / (1024**3), 2),
                        "free": is_free(item.discount),
                        "prefill_url": "/subscriptions/new?" + urlencode(prefill),
                    }
                )

    return _render(
        request,
        "search.html",
        keyword=keyword,
        mode=mode,
        category=group.key if group else "",
        sub=sub_selected,
        group_name=group.name if group else "",
        sort=sort_key,
        sort_label=sort_label(sort_key),
        discount=discount,
        discount_label=dict(DISCOUNT_OPTIONS).get(discount, ""),
        page_size=page_size,
        page=page,
        total_pages=total_pages,
        browse=browse,
        has_query=has_query,
        search_query=link(),
        prev_url=link(page=page - 1) if page > 1 else "",
        next_url=link(page=page + 1) if total_pages and page < total_pages else "",
        category_groups=CATEGORY_GROUPS,
        sort_options=[
            (key, label)
            for key, (_, _, label) in SORT_OPTIONS.items()
        ],
        discount_options=DISCOUNT_OPTIONS,
        page_sizes=PAGE_SIZES,
        results=results,
        total=total,
        error=error,
        retry_after=retry_after,
        subscriptions=subscriptions.list(),
    )


@router.post("/search/grab")
def search_grab(
    request: Request,
    mt_id: str = Form(...),
    name: str = Form(...),
    size_bytes: int = Form(0),
    seeders: int = Form(0),
    discount: str = Form(""),
    category: str = Form(""),
    publish_at: str = Form(""),
    subscription_id: int = Form(...),
    keyword: str = Form(""),
    back: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, downloader, _, _ = _services(request)
    candidate = back.strip()
    if (
        candidate.startswith("/search?")
        and "://" not in candidate
        and "\n" not in candidate
        and "\r" not in candidate
    ):
        back = candidate
    else:
        back = "/search?" + urlencode({"keyword": keyword})

    try:
        sub = subscriptions.get(subscription_id)
        torrent_id = _upsert_torrent(
            request,
            sub,
            mt_id=mt_id,
            name=name,
            size_bytes=size_bytes,
            seeders=seeders,
            discount=discount,
            category=category,
            publish_at=publish_at,
        )
        result = downloader.grab(torrent_id, actor=str(request.session.get("user")))
        level = "info" if result.outcome in {"grabbed", "dry_run"} else "warn"
        _flash(request, f"{result.message}（mt_id={mt_id}）", level)
    except (Conflict, NotFound, MTBoxError) as exc:
        _flash(request, str(exc), "error")
    except Exception as exc:  # defensive: surface, never 500 the form
        log.exception("手动抓取失败")
        _flash(request, f"抓取失败: {exc}", "error")
    return RedirectResponse(back, status_code=303)


def _upsert_torrent(
    request: Request,
    sub,
    *,
    mt_id: str,
    name: str,
    size_bytes: int,
    seeders: int,
    discount: str,
    category: str,
    publish_at: str,
) -> int:
    db = request.app.state.db
    with db.read() as conn:
        row = conn.execute("SELECT * FROM torrent WHERE mt_id = ?", (mt_id,)).fetchone()
    if row:
        if int(row["subscription_id"]) != sub.id:
            raise Conflict("该种子已归属其他订阅")
        return int(row["id"])

    parsed = parse_release(name)
    season = parsed.season or sub.season
    episode_start = episode_end = None
    if sub.kind == "tv" and parsed.episodes:
        episodes = [episode + sub.episode_offset for episode in parsed.episodes]
        episode_start, episode_end = min(episodes), max(episodes)
        if len(episodes) == 1:
            episode_end = None
    is_batch = bool(parsed.is_batch or (parsed.episodes and len(parsed.episodes) > 1))

    publish_iso = None
    parsed_publish = parse_dt(publish_at)
    if parsed_publish is not None:
        publish_iso = to_iso(parsed_publish)

    now = to_iso(utc_now())
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO torrent(mt_id, subscription_id, title, size_bytes, discount, seeders, "
            "category, publish_at, season, episode_start, episode_end, is_batch, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?) "
            "ON CONFLICT(mt_id) DO NOTHING",
            (
                mt_id,
                sub.id,
                name,
                max(0, size_bytes),
                discount or None,
                max(0, seeders),
                category or None,
                publish_iso,
                season,
                episode_start,
                episode_end,
                1 if is_batch else 0,
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute("SELECT * FROM torrent WHERE mt_id = ?", (mt_id,)).fetchone()
            if row and int(row["subscription_id"]) != sub.id:
                raise Conflict("该种子已归属其他订阅")
            return int(row["id"])
        return int(cursor.lastrowid)


# ---------- subscriptions ----------


@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request, view: str = "") -> Response:
    _require_user(request)
    subscriptions, _, _, _ = _services(request)
    # 已完成的订阅单独成页（?view=done），避免干扰进行中的订阅。
    done_view = view == "done"
    items = []
    active_count = done_count = 0
    for sub in subscriptions.list():
        is_done = sub.status == "done"
        if is_done:
            done_count += 1
        else:
            active_count += 1
        if is_done != done_view:
            continue
        items.append({"sub": sub, "progress": subscriptions.progress(sub.id)})
    return _render(
        request,
        "subscriptions.html",
        items=items,
        view="done" if done_view else "active",
        active_count=active_count,
        done_count=done_count,
    )


@router.get("/subscriptions/new", response_class=HTMLResponse)
def subscription_new_page(request: Request) -> Response:
    _require_user(request)
    params = request.query_params
    state = request.app.state
    prefill = {
        "kind": params.get("kind") or "tv",
        "title": params.get("title") or "",
        "original_title": params.get("original_title") or "",
        "year": params.get("year") or "",
        "season": params.get("season") or "1",
        "douban_id": params.get("douban_id") or "",
        "imdb_id": params.get("imdb_id") or "",
        "library_category": params.get("library_category") or "",
        "episode_offset": params.get("episode_offset") or "0",
        "interval_min": params.get("interval_min")
        or str(settings_store.get_int(state.db, "default_interval_min", 30)),
        "auto_grab": params.get("auto_grab")
        or ("1" if settings_store.get_bool(state.db, "auto_grab_default", False) else ""),
    }
    pending = _pending_torrent(
        mt_id=params.get("mt_id") or "",
        name=params.get("mt_name") or "",
        size_bytes=params.get("mt_size_bytes") or "",
        seeders=params.get("mt_seeders") or "",
        discount=params.get("mt_discount") or "",
        category=params.get("mt_category") or "",
        publish_at=params.get("mt_publish_at") or "",
    )
    return _render(request, "subscription_new.html", prefill=prefill, pending=pending)


@router.post("/subscriptions/new")
def subscription_create(
    request: Request,
    kind: str = Form("tv"),
    title: str = Form(""),
    original_title: str = Form(""),
    year: str = Form(""),
    season: int = Form(1),
    douban_id: str = Form(""),
    imdb_id: str = Form(""),
    library_category: str = Form(""),
    episode_offset: int = Form(0),
    episode_from: str = Form(""),
    episode_to: str = Form(""),
    interval_min: int = Form(30),
    auto_grab: str = Form(""),
    mt_id: str = Form(""),
    mt_name: str = Form(""),
    mt_size_bytes: str = Form(""),
    mt_seeders: str = Form(""),
    mt_discount: str = Form(""),
    mt_category: str = Form(""),
    mt_publish_at: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, downloader, _, _ = _services(request)
    try:
        sub = subscriptions.create(
            {
                "kind": kind,
                "title": title,
                "original_title": original_title or None,
                "year": int(year) if year.strip().isdigit() else None,
                "season": season,
                "douban_id": douban_id or None,
                "imdb_id": imdb_id or None,
                "library_category": library_category or None,
                "episode_offset": episode_offset,
                "episode_from": int(episode_from) if episode_from.strip().isdigit() else None,
                "episode_to": int(episode_to) if episode_to.strip().isdigit() else None,
                "interval_min": max(1, interval_min),
                "auto_grab": auto_grab == "on",
            }
        )
        message = f"订阅「{sub.title}」已创建"
        level = "info"
        pending = _pending_torrent(
            mt_id=mt_id,
            name=mt_name,
            size_bytes=mt_size_bytes,
            seeders=mt_seeders,
            discount=mt_discount,
            category=mt_category,
            publish_at=mt_publish_at,
        )
        if pending is not None:
            try:
                torrent_id = _upsert_torrent(request, sub, **pending)
                result = downloader.grab(
                    torrent_id, actor=str(request.session.get("user") or "manual")
                )
                level = "info" if result.outcome in {"grabbed", "dry_run"} else "warn"
                message = f"{message}；{result.message}"
            except (Conflict, NotFound, MTBoxError) as exc:
                level = "error"
                message = f"{message}；资源下载失败：{exc}"
            except Exception as exc:  # defensive: subscription is already saved
                log.exception("订阅创建后自动抓取失败")
                level = "error"
                message = f"{message}；资源下载失败：{exc}"
        _flash(request, message, level)
        return RedirectResponse("/subscriptions", status_code=303)
    except Conflict as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(request.url.path, status_code=303)


@router.get("/subscriptions/{sub_id}/edit", response_class=HTMLResponse)
def subscription_edit_page(request: Request, sub_id: int) -> Response:
    _require_user(request)
    subscriptions, _, _, _ = _services(request)
    try:
        sub = subscriptions.get(sub_id)
    except NotFound as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/subscriptions", status_code=303)
    return _render(request, "subscription_edit.html", sub=sub)


@router.post("/subscriptions/{sub_id}/edit")
def subscription_edit(
    request: Request,
    sub_id: int,
    title: str = Form(""),
    original_title: str = Form(""),
    year: str = Form(""),
    season: str = Form("1"),
    douban_id: str = Form(""),
    imdb_id: str = Form(""),
    library_category: str = Form(""),
    episode_offset: str = Form("0"),
    episode_from: str = Form(""),
    episode_to: str = Form(""),
    interval_min: str = Form("30"),
    auto_grab: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, _, _, _ = _services(request)
    patch = {
        "title": title,
        "original_title": original_title or None,
        "year": int(year) if year.strip().isdigit() else None,
        "season": max(1, int(season)) if season.strip().isdigit() else 1,
        "douban_id": douban_id or None,
        "imdb_id": imdb_id or None,
        "library_category": library_category or None,
        "episode_offset": int(episode_offset) if episode_offset.strip().lstrip("-").isdigit() else 0,
        "episode_from": int(episode_from) if episode_from.strip().isdigit() else None,
        "episode_to": int(episode_to) if episode_to.strip().isdigit() else None,
        "interval_min": max(1, int(interval_min)) if interval_min.strip().isdigit() else 30,
        "auto_grab": auto_grab == "on",
    }
    try:
        sub = subscriptions.update(sub_id, patch)
    except (Conflict, NotFound) as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/subscriptions/{sub_id}/edit", status_code=303)
    _flash(request, f"订阅「{sub.title}」已保存")
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/check")
def subscription_check(
    request: Request, sub_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    state = request.app.state
    lock = state.check_lock
    if not lock.acquire(blocking=False):
        _flash(request, "已有订阅检查在进行，请稍后再试", "warn")
        return RedirectResponse("/subscriptions", status_code=303)
    try:
        summary = state.search.check_subscription(sub_id, manual=True)
    finally:
        lock.release()
    if summary.error:
        _flash(request, f"检查失败：{summary.error}", "error")
    else:
        text = (
            f"检查完成：搜索 {summary.searched} 次，新候选 {summary.candidates}，"
            f"忽略 {summary.ignored}，抓取 {summary.grabbed}"
        )
        if summary.grab_note:
            text = f"{text}（{summary.grab_note}）"
        _flash(request, text, "warn" if summary.grab_note and not summary.grabbed else "info")
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/grab-best")
def subscription_grab_best(
    request: Request, sub_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    state = request.app.state
    try:
        result = state.search.grab_best(sub_id, actor=str(request.session.get("user")))
        if result is None:
            _flash(request, "没有满足条件的候选，先点“检查”抓取一次", "warn")
        else:
            if result.outcome in {"grabbed", "dry_run"}:
                level = "info"
            elif result.outcome == "skipped":
                level = "warn"
            else:
                level = "error"
            _flash(request, f"最优候选：{result.message}", level)
    except (Conflict, NotFound, MTBoxError) as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/status")
def subscription_status(
    request: Request,
    sub_id: int,
    status: str = Form(...),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, _, _, _ = _services(request)
    try:
        subscriptions.update(sub_id, {"status": status})
        _flash(request, "订阅状态已更新")
    except (Conflict, NotFound) as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/auto-grab")
def subscription_auto_grab(
    request: Request,
    sub_id: int,
    enabled: str = Form("0"),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, _, _, _ = _services(request)
    turn_on = enabled == "1"
    try:
        subscriptions.update(sub_id, {"auto_grab": turn_on})
        _flash(request, "已开启自动下载后续新集" if turn_on else "已关闭自动下载")
    except (Conflict, NotFound) as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/delete")
def subscription_delete(
    request: Request, sub_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    subscriptions, _, _, _ = _services(request)
    try:
        removed = subscriptions.delete(sub_id)
        message = "订阅已删除，关联任务已从任务页移除"
        if removed:
            message += f"，清理了 {removed} 条候选"
        _flash(request, message)
    except NotFound as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/subscriptions", status_code=303)


# ---------- tasks ----------


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, show: str = "", view: str = "") -> Response:
    _require_user(request)
    db = request.app.state.db
    # 已整理任务单独成页（?view=organized），任务页只留进行中/待处理的内容。
    organized_view = view == "organized"
    # 候选与已忽略属于筛选/候选页的内容，默认不进入任务列表；
    # ?show=all 可临时查看（例如清理残留记录）。
    show_all = show in {"all", "1", "true"} and not organized_view
    where = "t.state != 'deleted' AND s.deleted_at IS NULL"
    if organized_view:
        where += " AND t.state = 'organized'"
    else:
        where += " AND t.state != 'organized'"
        if not show_all:
            where += " AND t.state NOT IN ('candidate', 'ignored')"
    with db.read() as conn:
        rows = conn.execute(
            "SELECT t.*, s.title AS sub_title, s.kind AS sub_kind "
            "FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
            f"WHERE {where} "
            "ORDER BY t.updated_at DESC LIMIT 200"
        ).fetchall()
        hidden_count = conn.execute(
            "SELECT COUNT(*) FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
            "WHERE t.state IN ('candidate', 'ignored') AND s.deleted_at IS NULL"
        ).fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
            "WHERE t.state NOT IN ('candidate', 'ignored', 'organized', 'deleted') "
            "AND s.deleted_at IS NULL"
        ).fetchone()[0]
        organized_count = conn.execute(
            "SELECT COUNT(*) FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
            "WHERE t.state = 'organized' AND s.deleted_at IS NULL"
        ).fetchone()[0]
    tasks = [dict(row) for row in rows]
    for task in tasks:
        task["updated_display"] = local_display(task["updated_at"])
        task["size_gb"] = round(int(task["size_bytes"] or 0) / (1024**3), 2)
        task["episodes"] = _episode_label(task)
    return _render(
        request,
        "tasks.html",
        tasks=tasks,
        view="organized" if organized_view else "tasks",
        show_all=show_all,
        hidden_count=int(hidden_count or 0),
        active_count=int(active_count or 0),
        organized_count=int(organized_count or 0),
    )


@router.post("/tasks/poll")
def tasks_poll(request: Request, csrf_token: str = Form("")) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    _, _, poller, _ = _services(request)
    summary = poller.poll()
    if summary.error:
        _flash(request, f"轮询失败: {summary.error}", "error")
    else:
        _flash(
            request,
            f"轮询完成：{summary.polled} 个任务，完成 {summary.completed}，失败 {summary.failed}",
        )
    return RedirectResponse(request.headers.get("referer") or "/tasks", status_code=303)


@router.post("/tasks/organize-pending")
def tasks_organize_pending(request: Request, csrf_token: str = Form("")) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    _, _, _, organizer = _services(request)
    results = organizer.organize_pending()
    ok = sum(1 for result in results if result.ok)
    _flash(request, f"整理完成 {ok} / {len(results)}")
    return RedirectResponse(request.headers.get("referer") or "/tasks", status_code=303)


@router.post("/tasks/{torrent_id}/organize")
def task_organize(
    request: Request, torrent_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    _, _, _, organizer = _services(request)
    result = organizer.organize(torrent_id, force=True)
    _flash(request, result.message, "info" if result.ok else "error")
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{torrent_id}/retry")
def task_retry(
    request: Request, torrent_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    db = request.app.state.db
    with db.tx() as conn:
        cursor = conn.execute(
            "UPDATE torrent SET state = 'candidate', error = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'failed'",
            (to_iso(utc_now()), torrent_id),
        )
    _flash(request, "已重置为候选" if cursor.rowcount else "只有失败任务可以重试")
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{torrent_id}/delete")
def task_delete(
    request: Request, torrent_id: int, csrf_token: str = Form("")
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    state = request.app.state
    with state.db.read() as conn:
        row = conn.execute("SELECT * FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
    if not row:
        _flash(request, "任务不存在", "error")
        return RedirectResponse("/tasks", status_code=303)

    try:
        if row["qb_hash"]:
            state.qb.delete([str(row["qb_hash"])], delete_files=True)
        now = to_iso(utc_now())
        with state.db.tx() as conn:
            # User-initiated deletion always leaves the task list: keep the row
            # as ``deleted`` for audit instead of turning it into a failed task,
            # which would remain visible and look like the delete did nothing.
            conn.execute(
                "UPDATE torrent SET state = 'deleted', deleted_at = ?, "
                "error = COALESCE(error, '用户取消'), updated_at = ? WHERE id = ?",
                (now, now, torrent_id),
            )
            # Non-organized episodes may be grabbed again later; organized ones
            # are never reset.
            conn.execute(
                "DELETE FROM episode_state WHERE torrent_id = ? AND state != 'organized'",
                (torrent_id,),
            )
        log_event(
            state.db,
            "INFO",
            "download",
            "任务已删除",
            {"torrent_id": torrent_id, "previous_state": row["state"]},
        )
        _flash(request, "任务已删除")
    except MTBoxError as exc:
        _flash(request, f"删除失败: {exc}", "error")
    return RedirectResponse("/tasks", status_code=303)


# ---------- candidates ----------


def _candidate_subscriptions(db: Database) -> list[dict[str, object]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.kind, s.deleted_at, COUNT(t.id) AS candidate_count "
            "FROM subscription s LEFT JOIN torrent t "
            "ON t.subscription_id = s.id AND t.state IN ('candidate','ignored') "
            "GROUP BY s.id "
            "HAVING s.deleted_at IS NULL OR COUNT(t.id) > 0 "
            "ORDER BY s.id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def _candidate_sub_id(db: Database, raw: str) -> int | None:
    if not raw or not raw.strip().isdigit():
        return None
    sub_id = int(raw.strip())
    with db.read() as conn:
        row = conn.execute("SELECT 1 FROM subscription WHERE id = ?", (sub_id,)).fetchone()
    return sub_id if row else None


def _candidates_url(sub_id: int | None, view: str) -> str:
    params: list[str] = []
    if view != "best":
        params.append("view=all")
    if sub_id is not None:
        params.append(f"sub={sub_id}")
    return "/candidates" + (f"?{'&'.join(params)}" if params else "")


@router.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request) -> Response:
    _require_user(request)
    state = request.app.state
    view = request.query_params.get("view") or "best"
    if view not in {"best", "all"}:
        view = "best"
    subscriptions = _candidate_subscriptions(state.db)
    sub_id = _candidate_sub_id(state.db, request.query_params.get("sub") or "")
    selected = next((row for row in subscriptions if int(row["id"]) == sub_id), None)
    if sub_id is not None and selected is None:
        # A real subscription with no candidate rows of its own: show all
        # rather than an empty page with a filter the dropdown cannot show.
        sub_id = None
    matcher = Matcher(state.db)
    rows = (
        matcher.candidates(limit=500, subscription_id=sub_id)
        if view == "all"
        else matcher.best_candidates(subscription_id=sub_id)
    )
    items = []
    for row in rows:
        payload = candidate_payload(row)
        payload["episodes"] = _episode_label(payload)
        items.append(payload)
    total_count = sum(int(row["candidate_count"]) for row in subscriptions)
    scope_count = (
        int(selected["candidate_count"]) if selected is not None else total_count
    )
    return _render(
        request,
        "candidates.html",
        items=items,
        view=view,
        subscriptions=subscriptions,
        sub_id=sub_id,
        selected_sub=selected,
        total_count=total_count,
        scope_count=scope_count,
    )


@router.post("/candidates/delete")
def candidates_delete(
    request: Request,
    sub: str = Form(""),
    view: str = Form("best"),
    include_ignored: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    if view not in {"best", "all"}:
        view = "best"
    state = request.app.state
    raw_sub = sub.strip()
    sub_id: int | None = None
    if raw_sub:
        sub_id = _candidate_sub_id(state.db, raw_sub)
        if sub_id is None:
            # Never fall back to "all" when a specific filter was requested.
            _flash(request, "订阅筛选无效，未删除任何记录", "error")
            return RedirectResponse(_candidates_url(None, view), status_code=303)
    include = include_ignored == "1"
    deleted = Matcher(state.db).delete_candidates(sub_id, include_ignored=include)
    log_event(
        state.db,
        "INFO",
        "search",
        "批量删除候选",
        {"subscription_id": sub_id, "include_ignored": include, "deleted": deleted},
    )
    if deleted:
        suffix = "（含已忽略）" if include else ""
        _flash(request, f"已删除 {deleted} 条候选记录{suffix}")
    else:
        _flash(request, "筛选范围内没有可删除的候选")
    return RedirectResponse(_candidates_url(sub_id, view), status_code=303)


@router.post("/candidates/{torrent_id}/grab")
def candidate_grab(
    request: Request,
    torrent_id: int,
    csrf_token: str = Form(""),
    sub: str = Form(""),
    view: str = Form("best"),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    _, downloader, _, _ = _services(request)
    try:
        result = downloader.grab(torrent_id, actor=str(request.session.get("user")))
        _flash(request, result.message, "info" if result.outcome != "failed" else "error")
    except (Conflict, NotFound, MTBoxError) as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse(
        _candidates_url(_candidate_sub_id(request.app.state.db, sub), view),
        status_code=303,
    )


@router.post("/candidates/{torrent_id}/ignore")
def candidate_ignore(
    request: Request,
    torrent_id: int,
    csrf_token: str = Form(""),
    sub: str = Form(""),
    view: str = Form("best"),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    state = request.app.state
    try:
        Matcher(state.db).mark_manual_ignore(torrent_id)
        _flash(request, "已忽略该候选，自动检查不会再生效")
    except (Conflict, NotFound) as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse(
        _candidates_url(_candidate_sub_id(state.db, sub), view), status_code=303
    )


@router.post("/candidates/{torrent_id}/revive")
def candidate_revive(
    request: Request,
    torrent_id: int,
    csrf_token: str = Form(""),
    sub: str = Form(""),
    view: str = Form("best"),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed
    db = request.app.state.db
    with db.tx() as conn:
        cursor = conn.execute(
            "UPDATE torrent SET state = 'candidate', ignored_by = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'ignored'",
            (to_iso(utc_now()), torrent_id),
        )
    _flash(request, "已恢复为候选" if cursor.rowcount else "只有已忽略的候选可以恢复")
    return RedirectResponse(
        _candidates_url(_candidate_sub_id(db, sub), view), status_code=303
    )


def _digits(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _episode_label(task: dict[str, object]) -> str:
    start = task.get("episode_start")
    end = task.get("episode_end")
    if start is None:
        return "电影" if task.get("sub_kind") == "movie" else ""
    if end and end != start:
        return f"S{int(task.get('season') or 1):02d}E{int(start):02d}-E{int(end):02d}"
    return f"S{int(task.get('season') or 1):02d}E{int(start):02d}"


# ---------- settings ----------

INT_SETTINGS: dict[str, tuple[int, int]] = {
    "default_interval_min": (1, 1440),
    "max_candidates": (1, 500),
    "candidate_ttl_days": (1, 365),
    "season_pack_min_missing": (1, 100),
    "max_grabs_per_check": (1, 50),
    "new_episode_window_days": (1, 365),
    "min_seed_hours": (0, 100000),
    "max_seed_hours": (0, 100000),
    "daily_download_budget_gb": (0, 100000),
    "disk_pause_percent": (1, 99),
}
FLOAT_SETTINGS: dict[str, tuple[float, float]] = {
    "min_score": (0.0, 100.0),
    "min_ratio": (0.0, 100.0),
    "ratio_pause_nonfree": (0.0, 100.0),
    "ratio_pause_all": (0.0, 100.0),
}


def _validate_template(name: str, template: str) -> str | None:
    try:
        fields = {
            field
            for _, field, _, _ in string.Formatter().parse(template)
            if field is not None
        }
    except ValueError as exc:
        return f"{name} 格式非法: {exc}"
    unknown = fields - ALLOWED_PLACEHOLDERS
    if unknown:
        return f"{name} 含未知占位符: {', '.join(sorted(unknown))}"
    if "title" not in fields:
        return f"{name} 缺少 {{title}}"
    return None


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    _require_user(request)
    state = request.app.state
    values = settings_store.all_values(state.db)
    # Not stored by default; show the effective .env fallback in the form.
    values.setdefault(
        settings_store.SEARCH_DAILY_BUDGET_KEY,
        str(state.settings.mt_search_daily_budget),
    )
    values.setdefault(
        settings_store.SEARCH_MIN_INTERVAL_KEY,
        str(state.settings.mt_search_min_interval_sec),
    )
    # Never pass credentials to the template, even if a future field is added.
    for key in credentials.SECRET_KEYS:
        values.pop(key, None)
    return _render(
        request,
        "settings.html",
        values=values,
        mt_status=credentials.status(state.settings, state.db, credentials.MT_API_KEY),
        qb_status=credentials.status(state.settings, state.db, credentials.QB_PASSWORD),
    )


@router.post("/settings")
def settings_save(
    request: Request,
    default_interval_min: str = Form("30"),
    max_candidates: str = Form("20"),
    candidate_ttl_days: str = Form("7"),
    season_pack_min_missing: str = Form("3"),
    max_grabs_per_check: str = Form("6"),
    new_episode_window_days: str = Form("7"),
    min_seed_hours: str = Form("72"),
    max_seed_hours: str = Form("336"),
    daily_download_budget_gb: str = Form("50"),
    search_daily_budget: str = Form(""),
    search_min_interval_sec: str = Form(""),
    disk_pause_percent: str = Form("90"),
    min_score: str = Form("45"),
    min_ratio: str = Form("1.0"),
    ratio_pause_nonfree: str = Form("0.8"),
    ratio_pause_all: str = Form("0.6"),
    movie_path_template: str = Form(""),
    tv_path_template: str = Form(""),
    mode_map: str = Form("{}"),
    category_map: str = Form("{}"),
    notify_config: str = Form("{}"),
    auto_grab_default: str = Form(""),
    mt_api_key: str = Form(""),
    qb_password: str = Form(""),
    clear_mt_api_key: str = Form(""),
    clear_qb_password: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    _require_user(request)
    failed = _check_csrf(request, csrf_token)
    if failed:
        return failed

    raw_int = {
        "default_interval_min": default_interval_min,
        "max_candidates": max_candidates,
        "candidate_ttl_days": candidate_ttl_days,
        "season_pack_min_missing": season_pack_min_missing,
        "max_grabs_per_check": max_grabs_per_check,
        "new_episode_window_days": new_episode_window_days,
        "min_seed_hours": min_seed_hours,
        "max_seed_hours": max_seed_hours,
        "daily_download_budget_gb": daily_download_budget_gb,
        "disk_pause_percent": disk_pause_percent,
    }
    raw_float = {
        "min_score": min_score,
        "min_ratio": min_ratio,
        "ratio_pause_nonfree": ratio_pause_nonfree,
        "ratio_pause_all": ratio_pause_all,
    }

    updates: dict[str, str] = {}
    errors: list[str] = []
    for key, (low, high) in INT_SETTINGS.items():
        try:
            value = int(raw_int.get(key, ""))
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是整数")
            continue
        if not low <= value <= high:
            errors.append(f"{key} 必须在 {low}..{high}")
            continue
        updates[key] = str(value)
    for key, (low, high) in FLOAT_SETTINGS.items():
        try:
            number = float(raw_float.get(key, ""))
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是数字")
            continue
        if not low <= number <= high:
            errors.append(f"{key} 必须在 {low}..{high}")
            continue
        updates[key] = str(number)

    search_budget_text = search_daily_budget.strip()
    clear_search_budget = not search_budget_text
    if search_budget_text:
        try:
            search_budget = int(search_budget_text)
        except ValueError:
            errors.append("search_daily_budget 必须是整数")
        else:
            if not 1 <= search_budget <= 100000:
                errors.append("search_daily_budget 必须在 1..100000")
            else:
                updates[settings_store.SEARCH_DAILY_BUDGET_KEY] = str(search_budget)

    search_interval_text = search_min_interval_sec.strip()
    clear_search_interval = not search_interval_text
    if search_interval_text:
        try:
            search_interval = int(search_interval_text)
        except ValueError:
            errors.append("search_min_interval_sec 必须是整数")
        else:
            if not 0 <= search_interval <= 86400:
                errors.append("search_min_interval_sec 必须在 0..86400")
            else:
                updates[settings_store.SEARCH_MIN_INTERVAL_KEY] = str(search_interval)

    for key, template in (
        ("movie_path_template", movie_path_template),
        ("tv_path_template", tv_path_template),
    ):
        error = _validate_template(key, template)
        if error:
            errors.append(error)
        else:
            updates[key] = template.strip()

    for key, text in (("mode_map", mode_map), ("category_map", category_map)):
        try:
            parsed = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            errors.append(f"{key} 不是合法 JSON: {exc}")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"{key} 必须是 JSON 对象")
            continue
        if key == "mode_map" and not all(
            isinstance(value, str) for value in parsed.values()
        ):
            errors.append("mode_map 的值必须是字符串")
            continue
        if key == "category_map" and not all(
            isinstance(value, list) and all(isinstance(item, str) for item in value)
            for value in parsed.values()
        ):
            errors.append("category_map 的值必须是字符串数组")
            continue
        updates[key] = json.dumps(parsed, ensure_ascii=False)

    notify_error = notify_config_error(notify_config)
    if notify_error:
        errors.append(notify_error)
    else:
        updates["notify_config"] = json.dumps(
            json.loads(notify_config or "{}"), ensure_ascii=False
        )
    updates["auto_grab_default"] = "true" if auto_grab_default == "on" else "false"

    if not errors:
        pause_all = float(updates["ratio_pause_all"])
        pause_nonfree = float(updates["ratio_pause_nonfree"])
        if pause_all > pause_nonfree:
            errors.append("ratio_pause_all 不能大于 ratio_pause_nonfree")
        if int(updates["max_seed_hours"]) and int(updates["min_seed_hours"]) > int(
            updates["max_seed_hours"]
        ):
            errors.append("min_seed_hours 不能大于 max_seed_hours")

    new_secrets = {
        credentials.MT_API_KEY: mt_api_key.strip(),
        credentials.QB_PASSWORD: qb_password,
    }
    clear_secrets = {
        credentials.MT_API_KEY: clear_mt_api_key == "on",
        credentials.QB_PASSWORD: clear_qb_password == "on",
    }
    changed_secrets: list[str] = []
    for key, value in new_secrets.items():
        error = credentials.validate(value, key)
        if error:
            errors.append(error)
            continue
        if value and clear_secrets[key]:
            errors.append("不能同时填写并清除同一项凭据")
            continue
        if value or clear_secrets[key]:
            changed_secrets.append(key)

    if errors:
        _flash(request, "；".join(errors), "error")
        return RedirectResponse("/settings", status_code=303)

    db = request.app.state.db
    settings_store.set_values(db, updates)
    if clear_search_budget:
        # Empty field restores the .env fallback instead of pinning a value.
        settings_store.delete_values(db, [settings_store.SEARCH_DAILY_BUDGET_KEY])
    if clear_search_interval:
        settings_store.delete_values(db, [settings_store.SEARCH_MIN_INTERVAL_KEY])
    for key in changed_secrets:
        if new_secrets[key]:
            credentials.save(db, key, new_secrets[key])
        else:
            credentials.clear(db, key)
    if changed_secrets:
        # Restore .env values first, then re-apply the remaining overrides, so
        # clearing a saved credential really falls back instead of reusing the
        # value currently held in memory.
        state = request.app.state
        for key, base in state.base_credentials.items():
            setattr(state.settings, key, base)
        credentials.apply_overrides(state.settings, db)
        if credentials.MT_API_KEY in changed_secrets:
            state.mt.set_api_key(state.settings.mt_api_key)
        if credentials.QB_PASSWORD in changed_secrets:
            state.qb.set_password(state.settings.qb_password)
        register_secrets(state.settings.mt_api_key, state.settings.qb_password)

    log_event(
        db,
        "INFO",
        "settings",
        "运行时设置已更新",
        {"keys": sorted(updates), "credentials": sorted(changed_secrets)},
    )
    _flash(request, "设置已保存" if not changed_secrets else "设置与凭据已保存")
    return RedirectResponse("/settings", status_code=303)


# ---------- logs ----------


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> Response:
    _require_user(request)
    level = request.query_params.get("level") or None
    events = recent_events(request.app.state.db, limit=200, level=level)
    return _render(request, "logs.html", events=events, level=level or "")


# ---------- install ----------


def install(app) -> None:
    app.state.login_throttle = LoginThrottle()
    templates = app.state.templates

    def _display(value: object) -> str:
        return local_display(value, app.state.settings.tz)

    def _filesize(value: object) -> str:
        if value is None or value == "":
            return "—"
        try:
            size = float(value)
        except (TypeError, ValueError):
            return str(value)
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if size < 1024 or unit == "PB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
            size /= 1024
        return "—"

    templates.env.filters["localtime"] = _display
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["fromjson"] = lambda value: json.loads(value) if value else []

    async def login_required_handler(request: Request, _exc: Exception) -> Response:
        return _login_redirect(request)

    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _render(
                request, "error.html", status_code=404, code=404, message="页面不存在"
            )
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    app.add_exception_handler(LoginRequired, login_required_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.include_router(router)
