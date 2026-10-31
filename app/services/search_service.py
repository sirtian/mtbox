"""Subscription check: search M-Team, evaluate rules, persist candidates."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..clients.douban import DoubanClient, DoubanEpisodes, DoubanError
from ..clients.mteam import MTeamClient, MTeamSearchResult, MTeamTorrent
from ..clients.release_name import (
    parse_release,
    season_from_title,
    title_variants,
)
from ..config import Settings
from ..db import Database
from ..errors import (
    BudgetExceeded,
    CooldownActive,
    Conflict,
    MTeamApiError,
    MTeamAuthError,
    MTeamError,
    MTBoxError,
    NotFound,
    RateLimitBusy,
)
from ..models import Subscription
from ..timeutil import parse_dt, to_iso, utc_now
from . import settings_store
from .audit import log_event, recent_event
from .downloader import OUTCOME_SKIPPED, DownloadService, GrabResult
from .episodes import metadata_from_items
from .matcher import MatchResult, Matcher, aired_target, is_free
from .subscriptions import SubscriptionService

log = logging.getLogger("mtbox.search")

MAX_SEARCHES_PER_CHECK = 3


@dataclass(frozen=True, slots=True)
class CheckSummary:
    subscription_id: int
    searched: int = 0
    candidates: int = 0
    ignored: int = 0
    grabbed: int = 0
    error: str | None = None
    grab_note: str | None = None


class SearchService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        mt: MTeamClient,
        downloader: DownloadService | None = None,
        notifier: Any = None,
        douban: DoubanClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.mt = mt
        self.downloader = downloader
        self.notifier = notifier
        self.douban = douban
        self.subscriptions = SubscriptionService(db)
        self.matcher = Matcher(db, settings)

    # ---------- public ----------

    def check_due(self) -> list[CheckSummary]:
        return [self.check_subscription(sub.id) for sub in self.subscriptions.due()]

    def check_subscription(self, sub_id: int, *, manual: bool = False) -> CheckSummary:
        try:
            sub = self.subscriptions.get(sub_id)
        except NotFound as exc:
            return CheckSummary(sub_id, error=str(exc))
        if not manual and sub.status != "active":
            return CheckSummary(sub_id, error=f"订阅状态为 {sub.status}，跳过")

        try:
            return self._check(sub, manual=manual)
        except (RateLimitBusy, CooldownActive, BudgetExceeded) as exc:
            # Expected throttling: do not count it as a subscription failure.
            log.info("检查订阅 %s 被限流: %s", sub.id, exc)
            return CheckSummary(sub.id, error=str(exc))
        except MTeamAuthError as exc:
            self.subscriptions.mark_checked(sub.id, error=str(exc))
            first_alert = not recent_event(
                self.db, source="search", message="M-Team 认证失败"
            )
            log_event(self.db, "ERROR", "search", "M-Team 认证失败", {"subscription_id": sub.id})
            if first_alert:
                self._notify(
                    "mtbox API Key 失效",
                    "订阅检查时 M-Team 认证失败，请检查 MT_API_KEY。",
                    level="error",
                )
            return CheckSummary(sub.id, error=str(exc))
        except (MTeamError, MTBoxError) as exc:
            self.subscriptions.mark_checked(sub.id, error=str(exc))
            log_event(
                self.db,
                "WARN",
                "search",
                "订阅检查失败",
                {"subscription_id": sub.id, "error": str(exc)},
            )
            return CheckSummary(sub.id, error=str(exc))
        except Exception as exc:  # defensive: never leave the scheduler job stuck
            log.exception("订阅 %s 检查出现未预期错误", sub.id)
            self.subscriptions.mark_checked(sub.id, error=f"未预期错误: {exc}")
            return CheckSummary(sub.id, error=f"未预期错误: {exc}")

    # ---------- internals ----------

    def _notify(self, title: str, message: str, *, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(title, message, level=level)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.warning("发送通知失败", exc_info=True)

    def _check(self, sub: Subscription, *, manual: bool) -> CheckSummary:
        # A Douban-prefilled subscription starts with season=1 and only the
        # Chinese subject title.  Fix both before searching, otherwise the
        # "老友记 第二季" subject never matches a "Friends S02" release.
        sub, douban_episodes = self._prepare(sub)
        mode = self._mode(sub)
        categories = self._categories(sub)
        keywords = self._keywords(sub)
        searched = 0
        candidates = 0
        ignored = 0
        max_candidates = int(settings_store.get_int(self.db, "max_candidates", 20))
        min_score = settings_store.get_float(self.db, "min_score", 45.0)
        seen: set[str] = set()
        items: list[MTeamTorrent] = []

        for position, keyword in enumerate(keywords[:MAX_SEARCHES_PER_CHECK]):
            try:
                # Only the first keyword must be immediate.  Later ones may
                # wait for the 20s slot so a manual check still gets the
                # benefit of the original-title search.
                found = self._search(
                    keyword,
                    mode=mode,
                    categories=categories,
                    manual=manual and position == 0,
                )
            except (RateLimitBusy, CooldownActive, BudgetExceeded) as exc:
                # A manual check does not wait for the 20s search slot.  Keep
                # the results already collected instead of failing the whole
                # check when a second keyword cannot be searched yet.
                if position == 0:
                    raise
                log.info("订阅 %s 追加关键词 %s 被限流: %s", sub.id, keyword, exc)
                break
            searched += 1
            plausible = False
            for item in found.items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)
                if not plausible and self.matcher.item_usable(sub, item):
                    plausible = True
            # Another keyword only helps when this search found no release that
            # could actually be taken (e.g. only 4K packs over the daily
            # budget, while the other title form also has 1080p packs).
            if plausible:
                break

        # Refresh the total/latest episode before evaluating, so the first check
        # already targets the full season (and not only the last week's releases).
        self._sync_episodes(sub, items, douban_episodes)
        sub = self.subscriptions.get(sub.id)
        context = self.matcher.build_context(sub)

        for item in items:
            parsed = parse_release(item.name)
            result = self.matcher.evaluate(sub, item, parsed, context)
            stored = self._store(sub, item, result, min_score=min_score, limit=max_candidates)
            if stored == "candidate":
                candidates += 1
            elif stored == "ignored":
                ignored += 1

        grabbed, grab_note = self._auto_grab(sub)
        self.subscriptions.mark_checked(sub.id)
        log_event(
            self.db,
            "INFO",
            "search",
            f"检查订阅 {sub.title}",
            {
                "subscription_id": sub.id,
                "searched": searched,
                "candidates": candidates,
                "ignored": ignored,
                "grabbed": grabbed,
                "grab_note": grab_note,
                "total_episodes": sub.total_episodes,
                "available_episode": sub.available_episode,
            },
        )
        return CheckSummary(
            sub.id, searched, candidates, ignored, grabbed, grab_note=grab_note
        )

    def _prepare(
        self, sub: Subscription
    ) -> tuple[Subscription, DoubanEpisodes | None]:
        """Enrich a TV subscription from Douban before searching.

        Douban charts create one subject per season ("老友记 第二季"), but the
        prefill used to hardcode season 1.  The subject response also carries
        the original title and aliases, which are what "Friends S02" releases
        match on.  One lookup per check is shared with ``_sync_episodes``.
        """
        if sub.kind != "tv" or not sub.douban_id or self.douban is None:
            return sub, None
        needs_episodes = "豆瓣" not in (sub.episodes_source or "")
        needs_meta = not sub.original_title and not sub.aliases
        if not needs_episodes and not needs_meta:
            return sub, None
        try:
            episodes = self.douban.fetch_episodes(sub.douban_id)  # type: ignore[union-attr]
        except DoubanError as exc:
            log.info("订阅 %s 豆瓣信息读取失败: %s", sub.id, exc)
            return sub, None

        patch: dict[str, Any] = {}
        if episodes.original_title and not sub.original_title:
            patch["original_title"] = episodes.original_title
        merged = list(dict.fromkeys([*sub.aliases, *episodes.aliases]))
        if merged != list(sub.aliases):
            patch["aliases"] = merged[:10]
        if episodes.year and not sub.year:
            patch["year"] = episodes.year
        season = season_from_title(sub.title) or season_from_title(
            episodes.original_title
        )
        if season is not None and season != sub.season and not self._season_has_progress(sub):
            patch["season"] = season
            log_event(
                self.db,
                "INFO",
                "search",
                f"订阅 {sub.title} 季号校正为 S{season:02d}",
                {"subscription_id": sub.id, "old": sub.season, "new": season},
            )
        if patch:
            try:
                sub = self.subscriptions.update(sub.id, patch)
            except (Conflict, NotFound) as exc:
                log.info("订阅 %s 豆瓣信息未保存: %s", sub.id, exc)
        return sub, episodes

    def _season_has_progress(self, sub: Subscription) -> bool:
        """Do not rewrite a season that already has downloaded/known episodes."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM episode_state WHERE subscription_id = ? AND season = ? LIMIT 1",
                (sub.id, sub.season),
            ).fetchone()
        return row is not None

    def _sync_episodes(
        self,
        sub: Subscription,
        items: list[MTeamTorrent],
        episodes: DoubanEpisodes | None = None,
    ) -> None:
        """Update total/available episode counts from this search's results."""
        meta = metadata_from_items(sub, items)
        total = meta.total
        available = meta.available
        source = "PT" if total is not None else None

        # PT descriptions of an ongoing show usually only say "第 13-14 集";
        # some complete-pack descriptions also undercount the season.  Douban's
        # subject API has the planned total, so consult it once per subscription
        # (the "豆瓣" marker prevents repeated lookups) and keep the larger count.
        # `_prepare` may already have fetched the same subject; reuse it.
        needs_douban = bool(
            episodes is None
            and sub.douban_id
            and self.douban is not None
            and sub.kind == "tv"
            and "豆瓣" not in (sub.episodes_source or "")
        )
        if needs_douban:
            try:
                episodes = self.douban.fetch_episodes(sub.douban_id)  # type: ignore[union-attr]
            except DoubanError as exc:
                log.info("订阅 %s 豆瓣总集数读取失败: %s", sub.id, exc)
        if episodes is not None:
            if episodes.total:
                if total is None or episodes.total > total:
                    total = episodes.total
                    source = "豆瓣"
                else:
                    source = "PT+豆瓣"
            if episodes.available:
                available = max(available or 0, episodes.available)

        if total is None and available is None:
            return
        try:
            self.subscriptions.sync_episode_info(
                sub.id, total=total, available=available, source=source
            )
        except (Conflict, NotFound) as exc:
            log.info("订阅 %s 集数信息未保存: %s", sub.id, exc)

    def _search(
        self,
        keyword: str,
        *,
        mode: str,
        categories: list[str] | None,
        manual: bool,
    ) -> MTeamSearchResult:
        try:
            return self.mt.search(
                keyword,
                mode=mode,
                categories=categories,
                page_size=100,
                wait=not manual,
            )
        except MTeamApiError:
            if mode != "normal":
                log.info("mode=%s 搜索失败，回退 normal", mode)
                return self.mt.search(
                    keyword,
                    mode="normal",
                    categories=categories,
                    page_size=100,
                    wait=not manual,
                )
            raise

    def _keywords(self, sub: Subscription) -> list[str]:
        """Search terms, most useful first.

        PT names use the bare show name plus an Sxx marker ("Friends.S02"),
        while Douban subject titles carry the season ("老友记 第二季").  The
        base title therefore comes before the full one, and the original
        title before aliases.
        """
        keywords: list[str] = []

        def add(value: str) -> None:
            text = str(value or "").strip()
            if text and text not in keywords:
                keywords.append(text)

        def add_variants(value: str) -> None:
            variants = title_variants(value)
            for variant in variants[1:]:
                add(variant)
            add(value)

        add_variants(sub.title)
        add_variants(sub.original_title or "")
        for alias in sub.aliases:
            add_variants(alias)
        if sub.kind == "movie" and sub.year:
            add(f"{sub.title} {sub.year}")
        return keywords

    def _mode(self, sub: Subscription) -> str:
        mapping = settings_store.get_json(self.db, "mode_map", {}) or {}
        if isinstance(mapping, dict):
            value = mapping.get(sub.kind)
            if isinstance(value, str) and value:
                return value
        return "normal"

    def _categories(self, sub: Subscription) -> list[str] | None:
        mapping = settings_store.get_json(self.db, "category_map", {}) or {}
        if isinstance(mapping, dict):
            value = mapping.get(sub.kind)
            if isinstance(value, list) and value:
                return [str(item) for item in value]
        return None

    def _store(
        self,
        sub: Subscription,
        item: MTeamTorrent,
        result: MatchResult,
        *,
        min_score: float,
        limit: int,
    ) -> str:
        """Return 'candidate', 'ignored', or 'skipped'."""
        publish_at = parse_dt(item.created_at)
        publish_iso = to_iso(publish_at) if publish_at else None
        now = to_iso(utc_now())
        title_matched = any(rule.rule == "标题" and rule.passed for rule in result.rules)

        with self.db.read() as conn:
            row = conn.execute(
                "SELECT id, state, ignored_by FROM torrent WHERE mt_id = ?", (item.id,)
            ).fetchone()

        if row is not None:
            state = str(row["state"])
            if state == "ignored" and row["ignored_by"] == "manual":
                return "skipped"
            if state in {"grabbed", "downloading", "completed", "organized", "failed", "deleted"}:
                return "skipped"
            if result.passed and result.score >= min_score:
                self._update(int(row["id"]), item, result, "candidate", None, publish_iso, now)
                return "candidate"
            if state == "ignored" and not title_matched:
                return "skipped"
            new_state = (
                "candidate" if result.passed and result.score >= min_score else "ignored"
            )
            new_by = None if new_state == "candidate" else "auto"
            self._update(int(row["id"]), item, result, new_state, new_by, publish_iso, now)
            return new_state

        if not title_matched:
            return "skipped"
        if result.passed and result.score >= min_score:
            self._insert(sub, item, result, "candidate", None, publish_iso, now)
            return "candidate"
        if result.passed:
            # Score too low: keep for review, it can revive if a better row appears.
            log.debug("候选评分不足 %.1f: %s", result.score, item.name)
        ignored_count = self._count_auto_ignored(sub.id)
        if ignored_count >= limit:
            return "skipped"
        self._insert(sub, item, result, "ignored", "auto", publish_iso, now)
        return "ignored"

    def _update(
        self,
        torrent_id: int,
        item: MTeamTorrent,
        result: MatchResult,
        state: str,
        ignored_by: str | None,
        publish_iso: str | None,
        now: str,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET title = ?, size_bytes = ?, discount = ?, seeders = ?, "
                "category = ?, publish_at = ?, season = ?, episode_start = ?, episode_end = ?, "
                "is_batch = ?, score = ?, reasons = ?, state = ?, ignored_by = ?, error = NULL, "
                "updated_at = ? WHERE id = ?",
                (
                    item.name,
                    max(0, item.size_bytes),
                    item.discount,
                    max(0, item.seeders),
                    item.category,
                    publish_iso,
                    result.season,
                    result.episode_start,
                    result.episode_end,
                    1 if result.is_batch else 0,
                    result.score,
                    result.reasons_json(),
                    state,
                    ignored_by,
                    now,
                    torrent_id,
                ),
            )

    def _insert(
        self,
        sub: Subscription,
        item: MTeamTorrent,
        result: MatchResult,
        state: str,
        ignored_by: str | None,
        publish_iso: str | None,
        now: str,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO torrent(mt_id, subscription_id, title, size_bytes, discount, seeders, "
                "leechers, category, publish_at, season, episode_start, episode_end, is_batch, "
                "score, reasons, state, ignored_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mt_id) DO NOTHING",
                (
                    item.id,
                    sub.id,
                    item.name,
                    max(0, item.size_bytes),
                    item.discount,
                    max(0, item.seeders),
                    max(0, item.leechers),
                    item.category,
                    publish_iso,
                    result.season,
                    result.episode_start,
                    result.episode_end,
                    1 if result.is_batch else 0,
                    result.score,
                    result.reasons_json(),
                    state,
                    ignored_by,
                    now,
                    now,
                ),
            )

    def _count_auto_ignored(self, sub_id: int) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM torrent "
                "WHERE subscription_id = ? AND state = 'ignored' AND ignored_by = 'auto'",
                (sub_id,),
            ).fetchone()
        return int(row["n"])

    def grab_best(self, sub_id: int, *, actor: str = "manual") -> GrabResult | None:
        """Grab the best available candidate for a subscription, if any."""
        sub = self.subscriptions.get(sub_id)
        if self.downloader is None:
            raise Conflict("下载器未初始化")
        if sub.kind == "movie" and self._has_active_torrent(sub.id):
            raise Conflict("这部电影已有关联的下载/整理任务")
        rows = self._candidate_rows(sub.id)
        if not rows:
            return None
        plan = self.matcher.grab_plan(
            sub, rows, max_grabs=settings_store.get_int(self.db, "max_grabs_per_check", 6)
        )
        if not plan:
            return GrabResult(0, OUTCOME_SKIPPED, self._no_plan_note(sub))
        last_result: GrabResult | None = None
        grabbed: list[GrabResult] = []
        for row in plan:
            try:
                last_result = self.downloader.grab(int(row["id"]), actor=actor)
            except Conflict as exc:
                log.info("抓取最优候选跳过 %s: %s", row["id"], exc)
                continue
            except MTBoxError as exc:
                log.warning("抓取最优候选失败 %s: %s", row["id"], exc)
                continue
            if last_result.outcome in {"grabbed", "dry_run"}:
                grabbed.append(last_result)
        if not grabbed:
            return last_result
        first = grabbed[0]
        if len(grabbed) == 1:
            return first
        if first.outcome == "dry_run":
            message = f"DRY_RUN 已开启，未抓取（计划 {len(grabbed)} 个种子）"
        else:
            message = f"已抓取 {len(grabbed)} 个种子"
        return GrabResult(first.torrent_id, first.outcome, message)

    def _candidate_rows(self, sub_id: int) -> list[dict[str, Any]]:
        min_score = settings_store.get_float(self.db, "min_score", 45.0)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM torrent WHERE subscription_id = ? AND state = 'candidate' "
                "AND score >= ? ORDER BY score DESC, created_at DESC LIMIT 50",
                (sub_id, min_score),
            ).fetchall()
        return [dict(row) for row in rows]

    def _has_active_torrent(self, sub_id: int) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM torrent WHERE subscription_id = ? "
                "AND state IN ('grabbed','downloading','completed','organized') LIMIT 1",
                (sub_id,),
            ).fetchone()
        return row is not None

    def _auto_grab(self, sub: Subscription) -> tuple[int, str | None]:
        """Auto-grab everything aired that is still missing.

        Returns ``(grabbed, note)``.  A zero grab used to look identical to
        "nothing was found", which made a DRY_RUN instance or a switched-off
        subscription impossible to diagnose from the check result; the note
        names the reason so the flash message and event log explain it.
        """
        if not sub.auto_grab:
            return 0, "未开启自动下载"
        if self.downloader is None:
            return 0, "下载器未初始化"
        if sub.kind == "movie" and self._has_active_torrent(sub.id):
            return 0, "电影已有下载/整理任务"
        rows = self._candidate_rows(sub.id)
        if not rows:
            return 0, "没有达到评分线的候选"
        plan = self.matcher.grab_plan(
            sub, rows, max_grabs=settings_store.get_int(self.db, "max_grabs_per_check", 6)
        )
        if not plan:
            return 0, self._no_plan_note(sub)
        if self.settings.dry_run:
            return 0, f"DRY_RUN 已开启，计划抓取 {len(plan)} 个种子（未实际下载）"
        grabbed = 0
        last_error: str | None = None
        for row in plan:
            try:
                result = self.downloader.grab(int(row["id"]), actor="auto")
            except Conflict as exc:
                log.info("自动抓取跳过 %s: %s", row["id"], exc)
                last_error = str(exc)
                continue
            except MTBoxError as exc:
                log.warning("自动抓取失败 %s: %s", row["id"], exc)
                last_error = str(exc)
                continue
            log_event(
                self.db,
                "INFO" if result.outcome in {"grabbed", "dry_run"} else "WARN",
                "search",
                result.message,
                {"subscription_id": sub.id, "torrent_id": result.torrent_id},
            )
            if result.outcome in {"grabbed", "dry_run"}:
                grabbed += 1
            else:
                last_error = result.message
        if grabbed:
            return grabbed, None
        return 0, last_error or "抓取未成功"

    def _pending_episodes(self, sub: Subscription) -> set[int] | None:
        """Aired episodes still missing; ``None`` when the range is unknown."""
        aired = aired_target(sub)
        if aired is None:
            return None
        context = self.matcher.build_context(sub)
        return set(aired) - set(context.existing)

    def _no_plan_note(self, sub: Subscription) -> str:
        pending = self._pending_episodes(sub)
        if pending is None:
            return "没有可用的候选"
        if not pending:
            return "已播出剧集均已下载或在下载中"
        return f"还有 {len(pending)} 集待补，但没有可覆盖且在预算内的候选"


def candidate_payload(row: Any) -> dict[str, Any]:
    """Normalise a torrent row for templates."""
    payload = dict(row)
    try:
        payload["reasons"] = json.loads(payload.get("reasons") or "[]")
    except (TypeError, ValueError):
        payload["reasons"] = []
    payload["size_gb"] = round(int(payload.get("size_bytes") or 0) / (1024**3), 2)
    payload["free"] = is_free(payload.get("discount"))
    return payload
