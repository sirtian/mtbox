"""Hardlink completed downloads into the media library."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..clients.qbittorrent import QBittorrentClient
from ..clients.release_name import (
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ParsedRelease,
    parse_release,
)
from ..config import Settings
from ..db import Database
from ..errors import NotFound, QBError
from ..models import Subscription, Torrent
from ..timeutil import parse_dt, to_iso, utc_now
from . import settings_store
from .audit import log_event

log = logging.getLogger("mtbox.organizer")

EXCLUDED_WORDS = ("sample", "extras", "featurette", "behind", "trailer", "preview")
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_SEGMENT_BYTES = 180


@dataclass(frozen=True, slots=True)
class OrganizeResult:
    torrent_id: int
    ok: bool
    message: str
    linked: int = 0


class Organizer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        notifier: Any = None,
        qb: QBittorrentClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.qb = qb

    def organize_pending(self) -> list[OrganizeResult]:
        delay = self.settings.organize_delay_sec
        cutoff = to_iso(utc_now() - timedelta(seconds=delay))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id FROM torrent WHERE state = 'completed' AND completed_at <= ? "
                "UNION ALL "
                "SELECT id FROM torrent WHERE state = 'organize_failed' AND updated_at <= ? "
                "ORDER BY id LIMIT 20",
                (cutoff, to_iso(utc_now() - timedelta(minutes=30))),
            ).fetchall()
        return [self.organize(int(row["id"])) for row in rows]

    def organize(self, torrent_id: int, *, force: bool = False) -> OrganizeResult:
        torrent = self._get_torrent(torrent_id)
        if torrent.state == "organized" and not force:
            return OrganizeResult(torrent_id, True, "已整理", 0)
        if torrent.state not in {"completed", "organize_failed", "organized"}:
            return OrganizeResult(torrent_id, False, f"当前状态为 {torrent.state}，不能整理")

        if not force and not self._delay_elapsed(torrent):
            return OrganizeResult(torrent_id, False, "等待整理延迟")

        if self.settings.dry_run:
            log_event(
                self.db,
                "INFO",
                "organizer",
                f"DRY_RUN: would organize torrent {torrent_id}",
                {"torrent_id": torrent_id},
            )
            return OrganizeResult(torrent_id, True, "DRY_RUN 已开启，未整理", 0)

        created: list[Path] = []
        try:
            sub = self._get_subscription(torrent.subscription_id)
            linked = self._organize(
                torrent, sub, created, relocate=torrent.state == "organized"
            )
        except Exception as exc:
            for path in reversed(created):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("回滚链接失败: %s", path)
            error = str(exc) or exc.__class__.__name__
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE torrent SET state = 'organize_failed', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (error[:2000], to_iso(utc_now()), torrent_id),
                )
            log_event(
                self.db,
                "ERROR",
                "organizer",
                "整理失败",
                {"torrent_id": torrent_id, "error": error},
            )
            self._notify("mtbox 整理失败", f"任务 {torrent_id}: {error[:500]}", level="error")
            return OrganizeResult(torrent_id, False, error)
        return OrganizeResult(torrent_id, True, "整理完成", linked)

    # ---------- core ----------

    def _organize(
        self,
        torrent: Torrent,
        sub: Subscription,
        created: list[Path],
        *,
        relocate: bool = False,
    ) -> int:
        entries = [entry for entry in torrent.files if isinstance(entry, dict) and entry.get("path")]
        if not entries:
            raise ValueError("任务没有可用的文件列表")
        rels = [self._safe_relative(str(entry["path"])) for entry in entries]
        source_dir = self._source_dir(torrent, rels)

        # The recorded list can be stale (qB may hold a different release, or the
        # user replaced the files).  When no recorded video exists on disk, trust
        # what qBittorrent actually has for this torrent instead of failing.
        recorded_videos = [rel for rel in rels if is_video(rel)]
        if not any(self._entry_exists(source_dir, rel) for rel in recorded_videos):
            actual = self._actual_content(torrent)
            if actual is not None:
                log_event(
                    self.db,
                    "WARN",
                    "organizer",
                    "记录的文件与 qB 实际内容不一致，按实际文件整理",
                    {"torrent_id": torrent.id},
                )
                source_dir, entries = actual

        videos: list[tuple[str, int]] = []
        subtitles: list[str] = []
        for entry in entries:
            rel = self._safe_relative(str(entry["path"]))
            size = int(entry.get("size") or 0)
            if is_video(rel):
                if self._excluded(rel):
                    continue
                if size == 0:
                    try:
                        size = (source_dir / rel).stat().st_size
                    except OSError:
                        size = 0
                if size < self.settings.min_video_size_mb * 1024 * 1024:
                    continue
                videos.append((rel, size))
            elif is_subtitle(rel):
                subtitles.append(rel)
        if not videos:
            raise ValueError("没有可整理的视频文件")

        if sub.kind == "movie":
            rel, _ = max(videos, key=lambda item: item[1])
            mappings = {rel: []}
        else:
            mappings = self._resolve_episodes(sub, torrent, [rel for rel, _ in videos])

        mappings = self._drop_duplicate_episodes(mappings, dict(videos))

        library = self.settings.library_dir.resolve()
        library.mkdir(parents=True, exist_ok=True)
        template = settings_store.get_value(
            self.db,
            "movie_path_template" if sub.kind == "movie" else "tv_path_template",
        ) or ""
        if not template:
            raise ValueError("未配置命名模板")

        linked = 0
        organized_episodes: list[tuple[int, int]] = []
        targets: list[Path] = []
        legacy_targets: list[Path] = []
        for rel, size in videos:
            episodes = mappings.get(rel)
            if episodes is None:
                continue
            if sub.kind == "tv" and not episodes:
                raise ValueError(f"无法确定集数: {rel}")
            if sub.kind == "tv" and not relocate and self._all_organized(sub, torrent, episodes):
                continue

            src = self._locate_source(source_dir, rel)
            self._ensure_within(src, source_dir)

            parsed = parse_release(Path(rel).name)
            episode_start = episodes[0] if episodes else None
            contiguous = episodes == list(range(episodes[0], episodes[-1] + 1)) if episodes else False
            episode_end = episodes[-1] if contiguous and len(episodes) > 1 else None
            values = self._template_values(
                sub, torrent, parsed, episode_start, episode_end, rel
            )
            values["library"] = str(library)
            target = self._render_target(template, values, library)
            target, legacy = self._apply_category_dir(target, library, sub, template)
            if sub.kind == "tv" and episode_end is not None:
                target = self._apply_episode_range(target, episode_start, episode_end)
                if legacy is not None:
                    legacy = self._apply_episode_range(legacy, episode_start, episode_end)

            target = self._link(src, target, created)
            linked += 1
            targets.append(target)
            if legacy is not None:
                legacy_targets.append(legacy)
            self._link_subtitles(
                source_dir, src, target, subtitles, created, targets, legacy_targets, legacy
            )
            if sub.kind == "tv":
                season = torrent.season if torrent.season is not None else sub.season
                organized_episodes.extend((season, episode) for episode in episodes)

        self._remove_stale_links(torrent, targets, legacy_targets if relocate else [])

        now = to_iso(utc_now())
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE torrent SET state = 'organized', progress = 1.0, organized_at = ?, "
                "updated_at = ?, error = NULL, organized_paths = ? WHERE id = ?",
                (now, now, json.dumps([str(path) for path in targets], ensure_ascii=False), torrent.id),
            )
            for season, episode in organized_episodes:
                conn.execute(
                    "INSERT INTO episode_state(subscription_id, season, episode, torrent_id, state, updated_at) "
                    "VALUES (?, ?, ?, ?, 'organized', ?) "
                    "ON CONFLICT(subscription_id, season, episode) DO UPDATE SET "
                    "state = 'organized', torrent_id = excluded.torrent_id, updated_at = excluded.updated_at "
                    "WHERE episode_state.state != 'organized'",
                    (sub.id, season, episode, torrent.id, now),
                )
        self._maybe_finish_subscription(sub)
        log_event(
            self.db,
            "INFO",
            "organizer",
            f"整理完成: {torrent.title}",
            {"torrent_id": torrent.id, "linked": linked},
        )
        return linked

    # ---------- files ----------

    def _source_dir(self, torrent: Torrent, rels: list[str]) -> Path:
        """Base directory that the torrent file paths are relative to.

        qBittorrent is the source of truth: its content path already accounts for
        the category/save path and for the "create subfolder" content layout.
        The exact base varies with layout, so try the plausible bases and keep
        the one where the recorded files really resolve:

        - multi-file torrent:  content path is ``<save>/<name>``      -> base ``<save>``
        - single-file torrent: content path is ``<save>/<file>``      -> base ``<save>``
        - one-entry "files" torrent: qB still reports the file path
          ``<save>/<name>/<file>``  -> base must be ``<save>``, not ``<save>/<name>``
        - wrapped single-file: content path may be the wrapper folder -> base that folder
        """
        download_root = self.settings.download_dir.resolve()
        bases: list[Path] = []
        for raw in (self._live_content_path(torrent), torrent.content_path):
            bases.extend(self._content_bases(raw, download_root))

        try:
            save_base: Path | None = self._save_path_base(torrent)
        except (ValueError, FileNotFoundError):
            if not bases:
                raise
            save_base = None

        for base in bases:
            if self._entry_exists(base, *rels):
                return base
        if save_base is not None and (not bases or self._entry_exists(save_base, *rels)):
            return save_base
        if bases:
            return bases[0]
        assert save_base is not None
        return save_base

    def _save_path_base(self, torrent: Torrent) -> Path:
        download_root = self.settings.download_dir.resolve()
        raw = torrent.save_path or self.settings.qb_save_path
        try:
            local = self.settings.to_local_path(raw)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        resolved = local.resolve()
        self._ensure_within(resolved, download_root)
        if not resolved.is_dir():
            raise FileNotFoundError(f"下载目录不存在: {resolved}")
        return resolved

    def _live_content_path(self, torrent: Torrent) -> str | None:
        if self.qb is None or not torrent.qb_hash:
            return None
        try:
            items = self.qb.info([torrent.qb_hash])
        except QBError as exc:
            log.warning("读取 qBittorrent 实际路径失败: %s", exc)
            return None
        if not items:
            return None
        return items[0].content_path or None

    def _content_bases(self, raw: str | None, download_root: Path) -> list[Path]:
        if not raw:
            return []
        try:
            local = self.settings.to_local_path(raw).resolve()
        except ValueError:
            return []

        if local.is_dir():
            # Folder layout: base is normally the parent; a wrapped single-file
            # torrent may report the wrapper itself.
            candidates = [local.parent, local]
        else:
            # File layout (single-file, or a one-entry multi-file torrent):
            # base is the parent, or the grandparent when qB wrapped it in a
            # folder that the file list already contains.
            candidates = [local.parent, local.parent.parent]

        bases: list[Path] = []
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in bases:
                continue
            try:
                candidate.relative_to(download_root)
            except ValueError:
                continue
            if candidate.is_dir():
                bases.append(candidate)
        return bases

    def _actual_content(self, torrent: Torrent) -> tuple[Path, list[dict[str, Any]]] | None:
        """Files qBittorrent really has for this torrent (recorded list fallback)."""
        download_root = self.settings.download_dir.resolve()
        for raw in (self._live_content_path(torrent), torrent.content_path):
            if not raw:
                continue
            try:
                local = self.settings.to_local_path(raw).resolve()
            except ValueError:
                continue
            try:
                local.relative_to(download_root)
            except ValueError:
                continue
            if local == download_root:
                # Scanning the whole download root would sweep in other torrents.
                return None
            if local.is_file():
                return local.parent, [
                    {"path": local.name, "size": local.stat().st_size}
                ]
            if local.is_dir():
                entries = [
                    {"path": path.relative_to(local).as_posix(), "size": path.stat().st_size}
                    for path in sorted(local.rglob("*"))
                    if path.is_file()
                ]
                if entries:
                    return local, entries
        return None

    def _entry_exists(self, base: Path, *rels: str) -> bool:
        for rel in rels:
            if (base / rel).is_file():
                return True
            name = Path(rel).name
            if base.is_dir():
                for child in base.iterdir():
                    if child.is_dir() and (child / name).is_file():
                        return True
        return False

    def _locate_source(self, source_dir: Path, rel: str) -> Path:
        direct = source_dir / rel
        if direct.is_file():
            return direct
        name = Path(rel).name
        matches: list[Path] = []
        if source_dir.is_dir():
            for child in source_dir.iterdir():
                candidate = child / name
                if child.is_dir() and candidate.is_file():
                    matches.append(candidate)
        if len(matches) == 1:
            log.info("源文件在子目录中找到: %s", matches[0])
            return matches[0]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"源文件有多个同名候选，无法自动判断: {name}（{source_dir}）"
            )
        raise FileNotFoundError(self._missing_message(source_dir, rel))

    @staticmethod
    def _missing_message(source_dir: Path, rel: str) -> str:
        try:
            names = sorted(child.name for child in source_dir.iterdir())
        except OSError:
            names = []
        listing = "、".join(names[:10]) if names else "（空）"
        return f"源文件不存在: {rel}（下载目录: {source_dir}；目录内容: {listing}）"

    @staticmethod
    def _safe_relative(path: str) -> str:
        candidate = Path(path.replace("\\", "/"))
        if candidate.is_absolute() or any(part in ("..", "") for part in candidate.parts):
            raise ValueError(f"非法文件路径: {path}")
        return candidate.as_posix()

    @staticmethod
    def _ensure_within(path: Path, root: Path) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"路径越界: {path}") from exc

    @staticmethod
    def _excluded(path: str) -> bool:
        lowered = path.lower()
        return any(word in lowered for word in EXCLUDED_WORDS)

    def _link(self, src: Path, target: Path, created: list[Path]) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size == src.stat().st_size:
                return target
            target = self._unique_target(target)
        try:
            os.link(src, target)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.copy2(src, target)
                log_event(
                    self.db,
                    "WARN",
                    "organizer",
                    "跨存储池，已退化为复制",
                    {"source": str(src), "target": str(target)},
                )
            else:
                raise
        created.append(target)
        return target

    def _remove_stale_links(
        self, torrent: Torrent, targets: list[Path], legacy_targets: list[Path]
    ) -> None:
        """Drop library links that a re-organize no longer targets.

        Only paths previously recorded for this torrent (or the pre-category
        default location of the same file) are touched, and only inside
        ``LIBRARY_DIR``; the download copy is never affected.
        """
        library = self.settings.library_dir.resolve()
        keep = {path.resolve() for path in targets}
        candidates = [Path(str(raw)) for raw in torrent.organized_paths]
        candidates.extend(legacy_targets)
        for candidate in candidates:
            try:
                old = candidate.resolve()
            except OSError:
                continue
            if old in keep:
                continue
            try:
                old.relative_to(library)
            except ValueError:
                continue
            if not old.is_file():
                continue
            try:
                old.unlink()
                log.info("已移除旧位置的链接: %s", old)
            except OSError:
                log.warning("移除旧链接失败: %s", old)

    @staticmethod
    def _unique_target(target: Path) -> Path:
        for index in range(2, 1000):
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            if not candidate.exists():
                return candidate
        raise ValueError(f"无法找到可用的目标文件名: {target}")

    def _link_subtitles(
        self,
        source_dir: Path,
        video: Path,
        target: Path,
        subtitle_entries: list[str],
        created: list[Path],
        targets: list[Path],
        legacy_targets: list[Path],
        legacy: Path | None = None,
    ) -> None:
        stem = video.stem
        for rel in subtitle_entries:
            source = source_dir / rel
            if not source.is_file():
                # The video may have been located inside a qB "create subfolder"
                # wrapper; the subtitle sits next to it, not next to the base dir.
                sibling = video.parent / source.name
                if not sibling.is_file():
                    continue
                source = sibling
            if source.parent != video.parent:
                continue
            suffix = source.name[len(stem) :] if source.name.startswith(stem) else source.suffix
            if not source.name.startswith(stem):
                continue
            if not suffix.startswith("."):
                continue
            if Path("x" + suffix).suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            sub_target = target.with_name(target.stem + suffix)
            if sub_target.exists():
                targets.append(sub_target)
                if legacy is not None:
                    legacy_targets.append(legacy.with_name(sub_target.name))
                continue
            sub_target = self._link(source, sub_target, created)
            targets.append(sub_target)
            if legacy is not None:
                legacy_targets.append(legacy.with_name(sub_target.name))

    # ---------- episodes / naming ----------

    def _resolve_episodes(
        self, sub: Subscription, torrent: Torrent, videos: list[str]
    ) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        offset = sub.episode_offset
        for rel in videos:
            parsed = parse_release(Path(rel).name)
            if parsed.episodes:
                result[rel] = [episode + offset for episode in parsed.episodes]
        if result:
            return result

        if result:
            missing = [rel for rel in videos if rel not in result]
            if missing:
                raise ValueError(f"部分视频无法确定集数: {missing[0]}")
            return result

        covered = torrent.covered_episodes()
        if len(videos) == 1 and covered:
            return {videos[0]: covered}
        if covered and len(covered) == len(videos):
            ordered = sorted(videos, key=_natural_key)
            return {rel: [episode] for rel, episode in zip(ordered, covered)}
        raise ValueError("无法确定集数")

    def _drop_duplicate_episodes(
        self, mappings: dict[str, list[int]], sizes: dict[str, int]
    ) -> dict[str, list[int]]:
        seen: dict[tuple[int, ...], str] = {}
        cleaned: dict[str, list[int]] = {}
        for rel, episodes in mappings.items():
            key = tuple(episodes)
            if not episodes:
                cleaned[rel] = episodes
                continue
            if key in seen:
                keeper = seen[key]
                if sizes.get(rel, 0) > sizes.get(keeper, 0):
                    cleaned.pop(keeper, None)
                    seen[key] = rel
                    cleaned[rel] = episodes
                    log.warning("同一集存在多个文件，保留较大者: %s", rel)
                else:
                    log.warning("同一集存在多个文件，丢弃较小者: %s", rel)
                continue
            seen[key] = rel
            cleaned[rel] = episodes
        return cleaned

    def _all_organized(self, sub: Subscription, torrent: Torrent, episodes: list[int]) -> bool:
        season = torrent.season if torrent.season is not None else sub.season
        placeholders = ", ".join("?" for _ in episodes)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT episode FROM episode_state WHERE subscription_id = ? AND season = ? "
                f"AND state = 'organized' AND episode IN ({placeholders})",
                (sub.id, season, *episodes),
            ).fetchall()
        return len(rows) == len(episodes)

    def _template_values(
        self,
        sub: Subscription,
        torrent: Torrent,
        parsed: ParsedRelease,
        episode: int | None,
        episode_end: int | None,
        rel: str,
    ) -> dict[str, Any]:
        title = sub.title or sub.original_title or parsed.title_hint or torrent.title
        category = sub.library_category or ("电影" if sub.kind == "movie" else "电视剧")
        return {
            "library": str(self.settings.library_dir),
            "category": self._safe_segment(category),
            "title": self._safe_segment(title),
            "original_title": self._safe_segment(sub.original_title or title),
            "year": sub.year or "",
            "season": sub.season,
            "episode": episode if episode is not None else 0,
            "episode_end": episode_end if episode_end is not None else (episode or 0),
            "resolution": parsed.resolution or "",
            "video_codec": parsed.video_codec or "",
            "ext": Path(rel).suffix.lstrip(".").lower() or "mkv",
            "group": parsed.group or "",
        }

    def _render_target(self, template: str, values: dict[str, Any], library: Path) -> Path:
        rendered = template.format(**values)
        rendered = re.sub(r"\s*\(\s*\)", "", rendered)
        if not rendered.strip():
            raise ValueError("模板渲染结果为空")
        resolved = Path(rendered).resolve()
        try:
            relative = resolved.relative_to(library)
        except ValueError as exc:
            raise ValueError(f"目标路径越界: {resolved}") from exc
        parts = [self._safe_segment(part) for part in relative.parts]
        if not parts:
            raise ValueError("模板渲染结果为空")
        target = library / Path(*parts)
        self._ensure_within(target, library)
        return target

    def _apply_category_dir(
        self, target: Path, library: Path, sub: Subscription, template: str
    ) -> tuple[Path, Path | None]:
        """Let an explicit library category replace the stock ``Movies``/``TV`` dir.

        The stock templates start with the generic type folder and do not use
        ``{category}``.  When the subscription picks a category (动漫, 纪录片…)
        that folder should win, otherwise the choice would silently do nothing.
        Templates that already contain ``{category}`` are left untouched.
        """
        if not sub.library_category or "{category}" in template:
            return target, None
        kind_dir = "Movies" if sub.kind == "movie" else "TV"
        try:
            relative = target.relative_to(library)
        except ValueError:
            return target, None
        parts = relative.parts
        if len(parts) < 2 or parts[0] != kind_dir:
            return target, None
        category = self._safe_segment(sub.library_category)
        if category == kind_dir:
            return target, None
        new_target = library / category / Path(*parts[1:])
        self._ensure_within(new_target, library)
        return new_target, target

    @staticmethod
    def _apply_episode_range(target: Path, episode: int | None, episode_end: int) -> Path:
        if episode is None:
            return target
        marker = f"E{episode:02d}"
        if marker not in target.name:
            return target
        return target.with_name(target.name.replace(marker, f"{marker}-E{episode_end:02d}", 1))

    @staticmethod
    def _safe_segment(value: str) -> str:
        text = INVALID_CHARS.sub("", str(value)).strip(" .")
        if not text:
            return "untitled"
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_SEGMENT_BYTES:
            text = encoded[:MAX_SEGMENT_BYTES].decode("utf-8", errors="ignore").strip(" .")
        return text or "untitled"

    def _maybe_finish_subscription(self, sub: Subscription) -> None:
        if sub.status != "active":
            return
        done = False
        if sub.kind == "movie":
            done = True
        elif sub.episode_to is not None and sub.episode_from is not None:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM episode_state "
                    "WHERE subscription_id = ? AND season = ? AND state = 'organized' "
                    "AND episode BETWEEN ? AND ?",
                    (sub.id, sub.season, sub.episode_from, sub.episode_to),
                ).fetchone()
            done = int(row["n"]) >= sub.episode_to - sub.episode_from + 1
        if done:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE subscription SET status = 'done', updated_at = ? WHERE id = ?",
                    (to_iso(utc_now()), sub.id),
                )
            log_event(self.db, "INFO", "organizer", "订阅已完成", {"subscription_id": sub.id})

    def _delay_elapsed(self, torrent: Torrent) -> bool:
        completed = parse_dt(torrent.completed_at)
        if completed is None:
            return True
        return utc_now() - completed >= timedelta(seconds=self.settings.organize_delay_sec)

    # ---------- lookups ----------

    def _notify(self, title: str, message: str, *, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(title, message, level=level)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.warning("发送通知失败", exc_info=True)

    def _get_torrent(self, torrent_id: int) -> Torrent:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM torrent WHERE id = ?", (torrent_id,)).fetchone()
        if not row:
            raise NotFound(f"任务 {torrent_id} 不存在")
        return Torrent.from_row(row)

    def _get_subscription(self, sub_id: int) -> Subscription:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
        if not row:
            raise NotFound(f"订阅 {sub_id} 不存在")
        return Subscription.from_row(row)


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def is_subtitle(path: str) -> bool:
    return Path(path).suffix.lower() in SUBTITLE_EXTENSIONS


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]
