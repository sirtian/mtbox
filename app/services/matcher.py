"""Hard filters and scoring for subscription candidates (design §7.3–7.4)."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from ..clients.mteam import MTeamTorrent
from ..clients.release_name import (
    ParsedRelease,
    has_chinese_audio,
    normalize_title,
    parse_release,
    title_similarity,
    title_variants,
)
from ..db import Database
from ..errors import Conflict, NotFound
from ..models import Subscription
from ..timeutil import days_ago_iso, parse_dt, to_iso, utc_now
from . import profile_guard, settings_store
from .profile_guard import GuardState
from .storage import disk_usage

FREE_DISCOUNTS = {"FREE", "2XFREE", "2X_FREE", "_2X_FREE"}
ACTIVE_STATES = {"candidate", "grabbed", "downloading", "completed", "organized"}
DISPLAY_STATES = {"grabbed", "downloading", "completed", "organized"}

DEFAULT_QUALITY: dict[str, Any] = {
    "resolutions": ["2160p", "1080p"],
    "min_seeders": 1,
    "min_size_gb": 0,
    "max_size_gb": 0,
    "require_chinese_sub": False,
    "group_whitelist": [],
    "prefer_size_gb": 0,
    "allow_non_free": True,
    "exclude_words": [],
    # Selection preference, not a hard filter: 10bit releases and (for movies)
    # a Chinese dub / Mandarin audio track get a score bonus.
    "prefer_10bit": True,
    "prefer_chinese_audio": True,
}

RESOLUTION_RANK = {"2160p": 1.0, "1080p": 0.85, "720p": 0.5, "480p": 0.25}


def is_free(discount: str | None) -> bool:
    return (discount or "").strip().upper() in FREE_DISCOUNTS


def _budget_fits(row: dict[str, Any], used_gb: float, budget_gb: int) -> bool:
    """Whether another non-free candidate still fits the 24h budget."""
    if is_free(row.get("discount")) or not budget_gb:
        return True
    size_gb = int(row.get("size_bytes") or 0) / (1024**3)
    return used_gb + size_gb <= budget_gb


_DESC_HEAD = re.compile(r"[|｜\n]")
_DESC_SEP = re.compile(r"[/／;；、]")


def _candidate_titles(sub: Subscription) -> list[str]:
    """Title plus translated/original/alias forms, each with its base title.

    Douban stores per-season subjects ("老友记 第二季"), while PT names use
    "Friends.S02..." or "老友记.S02..."; the bare title is what matches.
    """
    values = [sub.title, sub.original_title or "", *sub.aliases]
    titles: list[str] = []
    for value in values:
        for variant in title_variants(value):
            if variant not in titles:
                titles.append(variant)
    return titles


def _description_similarity(titles: list[str], description: str) -> float:
    """Match against the alias list at the start of an M-Team description.

    Only the part before the first ``|`` is considered (the alias list), so
    genre/plot text cannot accidentally match a short title.  Segments must
    equal a candidate title, not merely contain it: "老友记" must not match
    "古宅老友记 第二季".
    """
    head = _DESC_HEAD.split(description, 1)[0]
    segments = {
        normalized
        for segment in _DESC_SEP.split(head)
        if (normalized := normalize_title(segment.strip(" -_.·:：")))
        and len(normalized) >= 2
    }
    for title in titles:
        normalized = normalize_title(title)
        if len(normalized) >= 2 and normalized in segments:
            return 1.0
    return 0.0


def episode_target(sub: Subscription) -> tuple[int, ...] | None:
    """Episode range a TV subscription wants.

    An explicit from/to wins.  With no explicit range, a PT/Douban-discovered
    total turns the subscription into a full-season target, so older missing
    episodes can be backfilled instead of only following the newest week.  An
    airing show often has no known total yet; its latest aired episode is then
    used as the upper bound, which also lets older aired releases pass the
    weekly recency window.  The target is what lets the ranking prefer a pack
    covering more missing episodes.
    """
    if sub.kind != "tv":
        return None
    total = sub.total_episodes if sub.total_episodes and 0 < sub.total_episodes <= 1000 else None
    available = (
        sub.available_episode
        if sub.available_episode and 0 < sub.available_episode <= 1000
        else None
    )
    upper_limit = total or available
    if sub.episode_from is not None:
        upper = (
            sub.episode_to
            if sub.episode_to is not None
            else (upper_limit or sub.episode_from)
        )
        if upper < sub.episode_from:
            return None
        return tuple(range(sub.episode_from, upper + 1))
    if upper_limit:
        return tuple(range(1, upper_limit + 1))
    return None


def aired_target(sub: Subscription) -> tuple[int, ...] | None:
    """Episodes that have actually aired, bounded by the subscription range.

    ``total_episodes`` is the planned season length; while a show is airing,
    only ``available_episode`` can be obtained.  Grab planning uses the aired
    range so every released episode is covered without waiting for the season
    to finish.  ``None`` means "no episode information yet"; an empty tuple
    means everything in the requested range is still unaired.
    """
    if sub.kind != "tv":
        return None
    total = (
        sub.total_episodes
        if sub.total_episodes and 0 < sub.total_episodes <= 1000
        else None
    )
    available = (
        sub.available_episode
        if sub.available_episode and 0 < sub.available_episode <= 1000
        else None
    )
    upper = available or total
    if sub.episode_to is not None:
        upper = sub.episode_to if upper is None else min(upper, sub.episode_to)
    lower = sub.episode_from if sub.episode_from is not None else 1
    if upper is None:
        return None
    if upper < lower:
        return ()
    return tuple(range(lower, upper + 1))


def has_chinese_subs(item: MTeamTorrent, parsed: ParsedRelease) -> bool:
    """Release names often omit 中字; M-Team labels carry the reliable hint."""
    if parsed.has_chinese_sub:
        return True
    return any(
        marker in label for label in item.labels for marker in ("中字", "简中", "繁中")
    )


def _chinese_audio(item: MTeamTorrent) -> bool:
    """Chinese dub marker in the release name, description or M-Team labels."""
    return has_chinese_audio(item.name, item.description, *item.labels)


def _douban_key(value: object) -> str:
    """Accept both '25853071' and a douban subject URL."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _imdb_key(value: object) -> str:
    match = re.search(r"tt\d+", str(value or ""), re.IGNORECASE)
    return match.group(0).lower() if match else ""


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class MatchContext:
    subscription: Subscription
    target: tuple[int, ...] | None
    existing: dict[int, str]
    downloaded_gb_24h: float
    guard: GuardState = GuardState()
    disk_percent: float | None = None
    disk_pause_percent: int = 90


@dataclass(frozen=True, slots=True)
class MatchResult:
    passed: bool
    score: float
    rules: tuple[RuleResult, ...]
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    is_batch: bool = False
    covered: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()

    @property
    def reason(self) -> str:
        for rule in reversed(self.rules):
            if not rule.passed:
                return f"{rule.rule}: {rule.detail}" if rule.detail else rule.rule
        return "通过"

    def reasons_json(self) -> str:
        return json.dumps([rule.as_dict() for rule in self.rules], ensure_ascii=False)


def _quality(sub: Subscription) -> dict[str, Any]:
    merged = dict(DEFAULT_QUALITY)
    merged.update({key: value for key, value in sub.quality.items() if value is not None})
    return merged


def _row_covered(row: dict[str, Any]) -> list[int]:
    try:
        start = int(row["episode_start"]) if row.get("episode_start") is not None else None
        end = int(row["episode_end"]) if row.get("episode_end") is not None else None
    except (TypeError, ValueError):
        return []
    if start is None or start <= 0:
        return []
    if end is None or end == start:
        return [start]
    if end < start:
        return []
    return list(range(start, end + 1))


def _as_int_set(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(result)))


class Matcher:
    def __init__(self, db: Database, settings: Any = None) -> None:
        self.db = db
        self.settings = settings

    # ---------- context ----------

    def build_context(self, sub: Subscription) -> MatchContext:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT episode, state FROM episode_state WHERE subscription_id = ? AND season = ?",
                (sub.id, sub.season),
            ).fetchall()
            budget_rows = conn.execute(
                "SELECT size_bytes, discount FROM torrent "
                "WHERE state IN ('grabbed','downloading','completed','organized') "
                "AND grabbed_at >= ?",
                (days_ago_iso(1),),
            ).fetchall()
        existing = {int(row["episode"]): str(row["state"]) for row in rows}
        downloaded = sum(
            int(row["size_bytes"] or 0)
            for row in budget_rows
            if not is_free(row["discount"])
        )
        target = episode_target(sub)
        disk_percent: float | None = None
        if self.settings is not None:
            usage = disk_usage(self.settings.library_dir)
            disk_percent = usage.percent if usage is not None else None
        return MatchContext(
            subscription=sub,
            target=target,
            existing=existing,
            downloaded_gb_24h=downloaded / (1024**3),
            guard=profile_guard.load(self.db),
            disk_percent=disk_percent,
            disk_pause_percent=settings_store.get_int(self.db, "disk_pause_percent", 90),
        )

    # ---------- evaluation ----------

    def evaluate(
        self,
        sub: Subscription,
        item: MTeamTorrent,
        parsed: ParsedRelease | None = None,
        context: MatchContext | None = None,
    ) -> MatchResult:
        parsed = parsed or parse_release(item.name)
        # Season packs are often named only "Show S02 2160p" without an episode
        # number.  The M-Team description usually carries "全24集", which gives
        # the pack a real episode range for targeting and ranking.
        if (
            sub.kind == "tv"
            and parsed.season is not None
            and not parsed.episodes
            and item.description
        ):
            extra = parse_release(item.description)
            if extra.is_batch and extra.episodes:
                parsed = replace(parsed, episodes=extra.episodes, is_batch=True)
        ctx = context or self.build_context(sub)
        quality = _quality(sub)
        rules: list[RuleResult] = []

        def fail(rule: str, detail: str) -> MatchResult:
            rules.append(RuleResult(rule, False, detail))
            return MatchResult(False, 0.0, tuple(rules), is_batch=parsed.is_batch)

        def passed(rule: str, detail: str = "") -> None:
            rules.append(RuleResult(rule, True, detail))

        # 1. visibility
        if not item.visible or item.banned:
            return fail("可见性", "种子不可见或被禁")
        passed("可见性")

        # 2. seeders
        min_seeders = int(quality.get("min_seeders") or 0)
        if item.seeders < min_seeders:
            return fail("做种数", f"做种数 {item.seeders} < {min_seeders}")
        passed("做种数", str(item.seeders))

        # 3. id consistency
        id_checked = self._check_ids(sub, item)
        if id_checked is False:
            return fail("ID", "豆瓣/IMDb 与订阅不一致")
        passed("ID", "种子未带 ID，按标题匹配" if id_checked is None else "ID 匹配")

        # 4. title: an exact Douban/IMDb ID beats a translated release name
        if id_checked is True:
            passed("标题", "ID 已匹配")
        else:
            threshold = 0.72 if id_checked is None else 0.60
            similarity = self._title_similarity(sub, item)
            if similarity < threshold:
                return fail("标题", f"相似度 {similarity:.2f} < {threshold:.2f}")
            passed("标题", f"{similarity:.2f}")

        # 5. year
        if sub.year and parsed.year and abs(parsed.year - sub.year) > 1:
            return fail("年份", f"{parsed.year} != {sub.year}")
        passed("年份", str(parsed.year or ""))

        # 6. kind
        is_batch = bool(parsed.is_batch)
        if (
            sub.kind == "tv"
            and not parsed.episodes
            and not is_batch
            and parsed.season is not None
        ):
            # "Show S02 2160p" with no episode number is a season pack.
            is_batch = True
        if sub.kind == "movie" and (parsed.episodes or is_batch):
            return fail("类型", "电影订阅不接受剧集种子")
        if sub.kind == "tv" and not parsed.episodes and not is_batch:
            return fail("类型", "剧集订阅需要集数或合集标记")
        passed("类型")

        # 7. season
        season = parsed.season if parsed.season is not None else sub.season
        if parsed.season is not None and parsed.season != sub.season:
            return fail("季", f"S{parsed.season:02d} != S{sub.season:02d}")
        if (
            parsed.season is None
            and sub.season > 1
            and sub.episode_offset == 0
            and sub.kind == "tv"
        ):
            return fail("季", "未解析出季，且未配置集数偏移")
        passed("季", f"S{season:02d}")

        # 8. episodes
        covered = tuple(
            sorted(
                {
                    episode + sub.episode_offset
                    for episode in parsed.episodes
                    if episode + sub.episode_offset > 0
                }
            )
        )
        missing: tuple[int, ...] = ()
        if sub.kind == "tv":
            if not covered and not is_batch:
                return fail("集数", "未解析出集数")
            if ctx.target is not None and covered:
                covered = tuple(episode for episode in covered if episode in ctx.target)
                if not covered:
                    return fail("集数", "解析出的集不在订阅范围内")
            if covered:
                missing = tuple(episode for episode in covered if episode not in ctx.existing)
            elif is_batch and ctx.target is not None:
                # Season pack whose episode count is unknown: assume it may
                # cover the whole target.  Coverage is not fabricated on the
                # torrent row, so episode_state still comes from real files.
                missing = tuple(episode for episode in ctx.target if episode not in ctx.existing)
            # Batches are rejected here if every covered episode exists; a
            # single already-present episode is reported by the dedupe rule.
            # Unknown-size packs have no ``covered``, so compare their target
            # instead, otherwise a finished season stays grabbable forever.
            if (covered and (is_batch or len(covered) > 1) and not missing) or (
                is_batch and ctx.target is not None and not missing
            ):
                return fail("集数", "覆盖的集已全部下载")
            min_missing = int(settings_store.get_int(self.db, "season_pack_min_missing", 3))
            # The threshold exists to avoid re-downloading a large pack for a
            # couple of new episodes.  A small pack (E07-E08) that brings two
            # genuinely missing episodes is a useful part of a multi-seed
            # plan, not waste, so only apply the threshold to larger packs.
            if (
                is_batch
                and ctx.target is not None
                and covered
                and len(covered) >= min_missing
                and len(missing) < min_missing
            ):
                return fail("集数", f"合集仅缺 {len(missing)} 集，少于阈值 {min_missing}")
        passed("集数", self._episode_text(covered, is_batch))

        # 9. size / exclusions
        size_gb = item.size_bytes / (1024**3)
        min_size = float(quality.get("min_size_gb") or 0)
        max_size = float(quality.get("max_size_gb") or 0)
        if min_size and size_gb < min_size:
            return fail("体积", f"{size_gb:.2f} GB < {min_size} GB")
        if max_size and size_gb > max_size:
            return fail("体积", f"{size_gb:.2f} GB > {max_size} GB")
        lowered = item.name.lower()
        for word in quality.get("exclude_words") or []:
            if word and str(word).lower() in lowered:
                return fail("体积", f"命中排除词 {word}")
        passed("体积", f"{size_gb:.2f} GB")

        # 10. resolution (2160p first) / bit depth, preference only
        preferred = [str(value).lower() for value in (quality.get("resolutions") or [])]
        resolution = (parsed.resolution or "").lower()
        if preferred and resolution and resolution not in preferred:
            return fail("分辨率", f"{parsed.resolution} 不在偏好 {preferred}")
        quality_detail = parsed.resolution or "未知"
        if parsed.bit_depth:
            quality_detail += f" {parsed.bit_depth}bit"
        passed("分辨率", quality_detail)

        # 11. chinese subtitles (release name or M-Team label)
        chinese_subs = has_chinese_subs(item, parsed)
        if quality.get("require_chinese_sub") and not chinese_subs:
            return fail("中字", "要求中字但未匹配到标记")
        passed("中字", "有" if chinese_subs else "未要求")

        # 12. audio track: overseas movies prefer a Chinese dub.  This is a
        # preference, never a rejection; native Chinese films simply may not
        # carry the 国语/国配 marker.
        chinese_audio = _chinese_audio(item)
        if sub.kind == "movie":
            passed("音轨", "中文配音" if chinese_audio else "未标注")

        # 13. duplicates
        duplicate = self._duplicate_reason(sub, item, covered)
        if duplicate:
            return fail("去重", duplicate)
        passed("去重")

        # 14. guard / disk / budget / free
        free = is_free(item.discount)
        if ctx.guard.paused:
            return fail("护栏", "分享率过低，已暂停抓取")
        if ctx.guard.free_only and not free:
            return fail("护栏", "分享率偏低，只允许免费种子")
        passed("护栏", ctx.guard.mode)
        if (
            not free
            and ctx.disk_percent is not None
            and ctx.disk_pause_percent
            and ctx.disk_percent >= ctx.disk_pause_percent
        ):
            return fail(
                "磁盘",
                f"磁盘水位 {ctx.disk_percent:.1f}% ≥ {ctx.disk_pause_percent}%，暂停非免费抓取",
            )
        passed("磁盘", "免费" if free else f"{ctx.disk_percent or 0:.1f}%")
        allow_non_free = bool(quality.get("allow_non_free", True))
        if not free:
            if not allow_non_free:
                return fail("预算", "非免费种子，订阅未允许")
            budget_gb = float(settings_store.get_int(self.db, "daily_download_budget_gb", 50))
            if budget_gb and ctx.downloaded_gb_24h + size_gb > budget_gb:
                return fail("预算", f"24h 下载预算不足 {ctx.downloaded_gb_24h:.1f}/{budget_gb:.0f} GB")
        passed("预算", "免费" if free else "非免费")

        # 15. recency window (only when no explicit range and no known total)
        if sub.kind == "tv" and sub.episode_from is None and ctx.target is None:
            window = int(settings_store.get_int(self.db, "new_episode_window_days", 7))
            published = parse_dt(item.created_at)
            if published is None or published < utc_now() - timedelta(days=window):
                return fail("追新窗口", f"发布于 {window} 天前或缺失，补老剧请填起始集")
        passed("追新窗口")

        episode_start = episode_end = None
        if covered:
            episode_start, episode_end = covered[0], covered[-1]
            if episode_start == episode_end:
                episode_end = None

        score = self._score(sub, item, parsed, quality)
        return MatchResult(
            True,
            score,
            tuple(rules),
            season=season,
            episode_start=episode_start,
            episode_end=episode_end,
            is_batch=is_batch,
            covered=covered,
            missing=missing,
        )

    # ---------- helpers ----------

    def item_plausible(self, sub: Subscription, item: MTeamTorrent) -> bool:
        """Whether an ID/title hit makes the item belong to this subscription."""
        checked = self._check_ids(sub, item)
        if checked is not None:
            return checked
        return self._title_similarity(sub, item) >= 0.72

    def item_usable(self, sub: Subscription, item: MTeamTorrent) -> bool:
        """Plausible and not already excluded by cheap hard filters.

        Used to decide whether another keyword is worth searching.  A 4K
        season pack over the daily budget must not stop the search for the
        smaller 1080p pack that a different keyword would return.
        """
        if not self.item_plausible(sub, item):
            return False
        quality = _quality(sub)
        if item.seeders < int(quality.get("min_seeders") or 0):
            return False
        size_gb = item.size_bytes / (1024**3)
        min_size = float(quality.get("min_size_gb") or 0)
        max_size = float(quality.get("max_size_gb") or 0)
        if min_size and size_gb < min_size:
            return False
        if max_size and size_gb > max_size:
            return False
        lowered = item.name.lower()
        for word in quality.get("exclude_words") or []:
            if word and str(word).lower() in lowered:
                return False
        parsed = parse_release(item.name)
        preferred = [str(value).lower() for value in (quality.get("resolutions") or [])]
        resolution = (parsed.resolution or "").lower()
        if preferred and resolution and resolution not in preferred:
            return False
        if not is_free(item.discount):
            if not bool(quality.get("allow_non_free", True)):
                return False
            budget = float(settings_store.get_int(self.db, "daily_download_budget_gb", 50))
            if budget and size_gb > budget:
                return False
        return True

    @staticmethod
    def _title_similarity(sub: Subscription, item: MTeamTorrent) -> float:
        titles = _candidate_titles(sub)
        aliases = titles[1:] if titles else []
        similarity = title_similarity(sub.title, aliases, item.name)
        if similarity < 1.0 and item.description:
            similarity = max(
                similarity, _description_similarity(titles, item.description)
            )
        return similarity

    @staticmethod
    def _check_ids(sub: Subscription, item: MTeamTorrent) -> bool | None:
        """False on mismatch, None when the seed carries no usable id, True on match."""
        compared = False
        sub_douban = _douban_key(sub.douban_id)
        item_douban = _douban_key(item.douban)
        if sub_douban and item_douban:
            compared = True
            if item_douban != sub_douban:
                return False
        sub_imdb = _imdb_key(sub.imdb_id)
        item_imdb = _imdb_key(item.imdb)
        if sub_imdb and item_imdb:
            compared = True
            if item_imdb != sub_imdb:
                return False
        return True if compared else None

    @staticmethod
    def _episode_text(covered: tuple[int, ...], is_batch: bool) -> str:
        if is_batch:
            return f"合集 {len(covered)} 集" if covered else "合集"
        if len(covered) == 1:
            return f"E{covered[0]:02d}"
        return f"E{covered[0]:02d}-E{covered[-1]:02d}" if covered else ""

    def _duplicate_reason(
        self, sub: Subscription, item: MTeamTorrent, covered: tuple[int, ...]
    ) -> str | None:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT mt_id, title, size_bytes, state, ignored_by, is_batch, "
                "episode_start FROM torrent WHERE subscription_id = ?",
                (sub.id,),
            ).fetchall()
        if sub.kind == "movie":
            for row in rows:
                if str(row["state"]) in ACTIVE_STATES:
                    return "电影已有关联任务"
        if sub.kind == "tv":
            # A season pack without a parsed episode range cannot mark
            # episode_state at grab time.  While it is still in flight, block
            # another pack instead of downloading the same season twice.
            for row in rows:
                if (
                    str(row["state"]) in {"grabbed", "downloading", "completed"}
                    and int(row["is_batch"] or 0)
                    and row["episode_start"] is None
                ):
                    return "已有整季合集正在下载"
        normalized = self._title_similarity(sub, item)
        for row in rows:
            if str(row["mt_id"]) == str(item.id):
                if row["state"] == "ignored" and row["ignored_by"] == "manual":
                    return "已被人工忽略"
                if row["state"] in ACTIVE_STATES:
                    return f"已存在（{row['state']}）"
                if row["state"] in {"failed", "organize_failed"}:
                    return None
            if row["state"] in ACTIVE_STATES and int(row["size_bytes"] or 0) == item.size_bytes:
                same_coverage = (
                    sub.kind == "movie"
                    or set(_row_covered(dict(row))) == set(covered)
                )
                if same_coverage and normalized >= 0.9 and str(row["title"]) != item.name:
                    return "同订阅存在相同体积的相似发布"
        if covered and len(covered) == 1:
            episode = covered[0]
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT state FROM episode_state WHERE subscription_id = ? AND season = ? "
                    "AND episode = ?",
                    (sub.id, sub.season, episode),
                ).fetchone()
            if row and str(row["state"]) in DISPLAY_STATES:
                return f"E{episode:02d} 已被其他任务覆盖"
        return None

    @staticmethod
    def _score(
        sub: Subscription, item: MTeamTorrent, parsed: ParsedRelease, quality: dict[str, Any]
    ) -> float:
        preferred = [str(value).lower() for value in (quality.get("resolutions") or [])]
        resolution = (parsed.resolution or "").lower()
        if preferred and resolution in preferred:
            rank = max(0.4, 1.0 - 0.3 * preferred.index(resolution))
        elif resolution:
            rank = RESOLUTION_RANK.get(resolution, 0.2)
        else:
            rank = 0.0
        score = 30.0 * rank
        if has_chinese_subs(item, parsed):
            score += 20.0
        # Soft preferences, sized below the 4K/1080p gap (9 points) so a 4K
        # release still outranks a 1080p one that happens to be 10bit / dubbed.
        if parsed.bit_depth == 10 and quality.get("prefer_10bit", True):
            score += 4.0
        if (
            sub.kind == "movie"
            and quality.get("prefer_chinese_audio", True)
            and _chinese_audio(item)
        ):
            score += 4.0
        if is_free(item.discount):
            score += 15.0
        score += 15.0 * min(1.0, math.log10(max(0, item.seeders) + 1) / 2)
        size_gb = item.size_bytes / (1024**3)
        prefer_size = float(quality.get("prefer_size_gb") or 0)
        if prefer_size > 0:
            closeness = max(0.0, 1.0 - abs(size_gb - prefer_size) / prefer_size)
            score += 10.0 * closeness
        else:
            score += 6.0
        published = parse_dt(item.created_at)
        if published is not None:
            age_days = max(0.0, (utc_now() - published).total_seconds() / 86400)
            recency = 1.0 if age_days <= 7 else max(0.0, 1.0 - (age_days - 7) / 23)
            score += 5.0 * recency
        whitelist = [str(value).lower() for value in (quality.get("group_whitelist") or [])]
        if parsed.group and parsed.group.lower() in whitelist:
            score += 5.0
        return round(score, 1)

    def candidates(
        self, limit: int = 100, subscription_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["t.state IN ('candidate','ignored')"]
        params: list[Any] = []
        if subscription_id is not None:
            clauses.append("t.subscription_id = ?")
            params.append(subscription_id)
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT t.*, s.title AS sub_title, s.kind AS sub_kind "
                "FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY (t.state = 'candidate') DESC, t.score DESC, t.created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def best_candidates(
        self, limit: int = 500, subscription_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the best candidate set for each subscription.

        The UI uses this by default so a subscription does not dump every
        matching release on the user.  For a TV show whose remaining aired
        episodes need more than one release (for example an E01-E18 pack plus
        an E19-E20 pack), every torrent in the complete ``grab_plan`` is
        returned.  The page intentionally ignores the per-check grab cap and
        daily budget so the combination covers the whole aired gap; the
        actual auto-grab still takes capped, budgeted batches.  The full
        ranking stays available for the "show all" view.
        """
        clauses = ["t.state = 'candidate'", "s.deleted_at IS NULL"]
        params: list[Any] = []
        if subscription_id is not None:
            clauses.append("t.subscription_id = ?")
            params.append(subscription_id)
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT t.*, s.title AS sub_title, s.kind AS sub_kind "
                "FROM torrent t JOIN subscription s ON s.id = t.subscription_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY t.score DESC, t.created_at DESC LIMIT ?",
                params,
            ).fetchall()
            sub_ids = sorted({int(row["subscription_id"]) for row in rows})
            subscriptions: dict[int, Subscription] = {}
            if sub_ids:
                placeholders = ", ".join("?" for _ in sub_ids)
                sub_rows = conn.execute(
                    f"SELECT * FROM subscription WHERE id IN ({placeholders})",
                    sub_ids,
                ).fetchall()
                subscriptions = {
                    int(sub_row["id"]): Subscription.from_row(sub_row)
                    for sub_row in sub_rows
                }

        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["subscription_id"]), []).append(dict(row))

        groups: list[tuple[float, str, list[dict[str, Any]]]] = []
        for sub_id, items in grouped.items():
            sub = subscriptions.get(sub_id)
            if sub is None:
                continue
            ranked = self.rank_candidates(sub, items)
            if not ranked:
                continue
            # The grab plan is the set of torrents that together cover every
            # still-missing aired episode.  The page shows the complete set,
            # not just one capped/budgeted batch: the user asked for every
            # missing episode here, and the next checks would grab the rest
            # anyway.  If it is empty (everything is already downloaded, or no
            # candidate can fill the gap) keep the previous single-best
            # behaviour for visibility.
            plan = self.grab_plan(
                sub,
                items,
                max_grabs=len(items) or 1,
                ignore_limits=True,
            )
            chosen = plan or ranked[:1]
            plan_count = len(plan)
            alt_count = len(ranked) - len(chosen)
            for index, row in enumerate(chosen, start=1):
                row["alt_count"] = max(0, alt_count)
                row["ranked_count"] = len(ranked)
                row["plan_count"] = plan_count
                row["plan_index"] = index
            groups.append(
                (
                    float(chosen[0].get("score") or 0),
                    str(chosen[0].get("publish_at") or ""),
                    chosen,
                )
            )
        groups.sort(key=lambda group: (group[0], group[1]), reverse=True)
        result: list[dict[str, Any]] = []
        for _, _, chosen in groups:
            result.extend(chosen)
            if len(result) >= limit:
                break
        return result

    def rank_candidates(
        self, sub: Subscription, rows: list[Any]
    ) -> list[dict[str, Any]]:
        """Annotate and order candidates for one subscription.

        For a TV subscription with a known range, the candidate covering the
        most still-missing **aired** episodes wins first; score, free status,
        seeders and publish time break ties.  Unaired episodes must not count
        as a gap, otherwise an airing show with 20 of 30 episodes released
        looks 30 episodes short.

        A pack whose title only says "S01"/"Complete" has no parsed episode
        range, so its real coverage is unknown.  It keeps its potential gap
        for display but is sorted after every candidate whose episodes are
        known: uploaders do mislabel single episodes as "S01 Complete", and
        letting such a pack satisfy the whole gap hid every other seed from
        the plan.  Without a known range (weekly following), quality score
        leads.
        """
        with self.db.read() as conn:
            state_rows = conn.execute(
                "SELECT episode FROM episode_state WHERE subscription_id = ? AND season = ?",
                (sub.id, sub.season),
            ).fetchall()
        existing = {int(row["episode"]) for row in state_rows}
        target_values = episode_target(sub)
        target = set(target_values) if target_values is not None else None
        aired_values = aired_target(sub)
        aired = set(aired_values) if aired_values is not None else None

        ranked: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            covered = _row_covered(data)
            if target is not None:
                scoped = [episode for episode in covered if episode in target]
            else:
                scoped = covered
            missing = [
                episode
                for episode in scoped
                if episode not in existing and (aired is None or episode in aired)
            ]
            unknown_coverage = bool(
                not scoped and int(data.get("is_batch") or 0) and target is not None
            )
            if unknown_coverage:
                potential = aired if aired is not None else target
                missing = [
                    episode for episode in (potential or ()) if episode not in existing
                ]
            data["coverage"] = len(scoped)
            data["missing_count"] = len(missing)
            data["coverage_unknown"] = unknown_coverage
            data["covered_episodes"] = scoped
            ranked.append(data)

        prefer_coverage = sub.kind == "tv" and target is not None

        def sort_key(data: dict[str, Any]) -> tuple[Any, ...]:
            score = float(data.get("score") or 0)
            free = 1 if is_free(data.get("discount")) else 0
            seeders = int(data.get("seeders") or 0)
            publish = str(data.get("publish_at") or "")
            if prefer_coverage:
                return (
                    0 if data.get("coverage_unknown") else 1,
                    int(data.get("missing_count") or 0),
                    score,
                    free,
                    seeders,
                    publish,
                    int(data["id"]),
                )
            return (score, free, seeders, publish, int(data["id"]))

        ranked.sort(key=sort_key, reverse=True)
        return ranked

    def grab_plan(
        self,
        sub: Subscription,
        rows: list[Any],
        *,
        max_grabs: int = 6,
        ignore_limits: bool = False,
    ) -> list[dict[str, Any]]:
        """Pick the candidates that together cover every aired episode.

        A cumulative pack with known coverage ("全24集" / "更新至14集" /
        "S01E01-E06") still wins on its own.  When only partial packs or
        single episodes are available, the best candidate for the
        still-uncovered aired episodes is added until everything is covered,
        the grab cap is reached, or the daily budget runs out.  An
        unknown-coverage pack ("Show S01 Complete" without an episode range)
        is only used after every verifiable candidate has been considered,
        because its actual contents cannot be trusted.

        ``ignore_limits=True`` returns the full coverage set for display:
        the candidate page must show every seed needed to fill the aired gap,
        while the actual grab still runs in capped, budgeted batches.
        """
        ranked = self.rank_candidates(sub, rows)
        if not ranked:
            return []
        if sub.kind != "tv":
            return ranked[:1]
        aired = aired_target(sub)
        if aired is None:
            return ranked[:1]
        if not aired:
            return []

        context = self.build_context(sub)
        pending = set(aired) - set(context.existing)
        if not pending:
            return []
        budget_gb = (
            0
            if ignore_limits
            else settings_store.get_int(self.db, "daily_download_budget_gb", 50)
        )
        used_gb = context.downloaded_gb_24h
        limit = max(1, int(max_grabs))

        plan: list[dict[str, Any]] = []
        for row in ranked:
            if len(plan) >= limit:
                break
            covered = {int(episode) for episode in (row.get("covered_episodes") or [])}
            if not covered:
                if not int(row.get("is_batch") or 0):
                    continue
                # Unknown-size cumulative pack, considered only after every
                # verifiable candidate: take it as a last resort and let the
                # real files fill episode_state.  It is not allowed to hide
                # known partial packs behind a "Complete" label.
                if not _budget_fits(row, used_gb, budget_gb):
                    continue
                plan.append(row)
                break
            adds = covered & pending
            if not adds:
                continue
            if not _budget_fits(row, used_gb, budget_gb):
                continue
            plan.append(row)
            pending -= adds
            if not is_free(row.get("discount")):
                used_gb += int(row.get("size_bytes") or 0) / (1024**3)
            if not pending:
                break
        return plan

    def delete_candidates(
        self,
        subscription_id: int | None = None,
        *,
        include_ignored: bool = False,
    ) -> int:
        """Delete candidate rows, optionally scoped to one subscription.

        Never touches grabbed/downloading/organized tasks: only rows still in
        the candidate (and, when requested, ignored) state are removed.
        """
        states = ["candidate"]
        if include_ignored:
            states.append("ignored")
        placeholders = ", ".join("?" for _ in states)
        sql = f"DELETE FROM torrent WHERE state IN ({placeholders})"
        params: list[Any] = list(states)
        if subscription_id is not None:
            sql += " AND subscription_id = ?"
            params.append(subscription_id)
        with self.db.tx() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.rowcount or 0)

    def mark_manual_ignore(self, torrent_id: int) -> None:
        with self.db.tx() as conn:
            row = conn.execute("SELECT state FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
            if not row:
                raise NotFound(f"任务 {torrent_id} 不存在")
            if str(row["state"]) not in {"candidate", "ignored"}:
                raise Conflict(f"当前状态为 {row['state']}，不能忽略")
            conn.execute(
                "UPDATE torrent SET state = 'ignored', ignored_by = 'manual', updated_at = ? "
                "WHERE id = ?",
                (to_iso(utc_now()), torrent_id),
            )
