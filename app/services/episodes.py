"""Discover a TV subscription's total/latest episode from PT search results.

M-Team release descriptions usually carry either a complete-series marker
("全 46 集") or the episode range ("第 13-14 集").  Those are enough to keep a
per-subscription episode summary without asking for another metadata service.
Douban is only a fallback for an ongoing show whose PT releases do not state a
total yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..clients.mteam import MTeamTorrent
from ..clients.release_name import ParsedRelease, parse_release, title_similarity
from ..models import Subscription

_TOTAL = re.compile(
    r"(?:全|共)\s*(\d{1,4})\s*[集话話期]|(?<!\d)(\d{1,4})\s*[集话話]\s*全"
)
_DESC_RANGE = re.compile(r"第?\s*(\d{1,4})\s*[-~至]\s*(\d{1,4})\s*[集话話期]")
_DESC_SINGLE = re.compile(r"(?:更新至|已更新至|第)\s*第?\s*(\d{1,4})\s*[集话話期]")
_MAX_EPISODES = 1000


@dataclass(frozen=True, slots=True)
class EpisodeMetadata:
    total: int | None = None
    available: int | None = None


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _imdb(value: object) -> str:
    match = re.search(r"tt\d+", str(value or ""), re.IGNORECASE)
    return match.group(0).lower() if match else ""


def extract_total(*texts: str) -> int | None:
    """Return a complete-series count from a release name/description."""
    for text in texts:
        for match in _TOTAL.finditer(str(text or "")):
            raw = match.group(1) or match.group(2)
            try:
                count = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 < count <= _MAX_EPISODES:
                return count
    return None


def extract_available(text: str) -> int | None:
    """Return the highest episode mentioned in a description, if any."""
    best: int | None = None
    for match in _DESC_RANGE.finditer(text or ""):
        try:
            best = max(best or 0, int(match.group(1)), int(match.group(2)))
        except (TypeError, ValueError):
            continue
    for match in _DESC_SINGLE.finditer(text or ""):
        try:
            best = max(best or 0, int(match.group(1)))
        except (TypeError, ValueError):
            continue
    return best if best and best <= _MAX_EPISODES else None


def _belongs_to(sub: Subscription, item: MTeamTorrent, parsed: ParsedRelease) -> bool:
    sub_douban = _digits(sub.douban_id)
    item_douban = _digits(item.douban)
    compared = False
    if sub_douban and item_douban:
        compared = True
        if sub_douban != item_douban:
            return False
    sub_imdb = _imdb(sub.imdb_id)
    item_imdb = _imdb(item.imdb)
    if sub_imdb and item_imdb:
        compared = True
        if sub_imdb != item_imdb:
            return False

    if parsed.season is not None and parsed.season != sub.season:
        return False
    if parsed.season is None and sub.season > 1:
        return False
    if sub.year and parsed.year and abs(parsed.year - sub.year) > 1:
        return False
    if not compared:
        names = [sub.title]
        if sub.original_title:
            names.append(sub.original_title)
        names.extend(sub.aliases)
        # English release names plus the Chinese metadata in smallDescr give
        # the best chance of recognising a translated show title.
        haystack = f"{item.name} {item.description}".strip()
        if title_similarity(sub.title, names[1:], haystack) < 0.70:
            return False
    return True


def metadata_from_items(
    sub: Subscription, items: list[MTeamTorrent] | tuple[MTeamTorrent, ...]
) -> EpisodeMetadata:
    """Collect total/available episode counts from matching PT search rows."""
    if sub.kind != "tv":
        return EpisodeMetadata()
    total: int | None = None
    available: int | None = None
    for item in items:
        parsed = parse_release(item.name)
        item_total = extract_total(item.name, item.description)
        if not parsed.episodes and not parsed.is_batch and item_total is None:
            continue
        if not _belongs_to(sub, item, parsed):
            continue
        if item_total is not None:
            total = max(total or 0, item_total)

        number = max(parsed.episodes) if parsed.episodes else None
        if number is None:
            number = extract_available(item.description)
        if number is None and parsed.is_batch and item_total is not None:
            number = item_total
        if number is not None:
            number += sub.episode_offset
            if 0 < number <= _MAX_EPISODES:
                available = max(available or 0, number)

    if total is not None and available is not None:
        available = min(available, total)
    return EpisodeMetadata(total=total, available=available)
