"""Release-name parsing and title matching.

Kept pure and dependency-light so it can be unit-tested without network or DB.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

try:  # optional, but bundled: pure Python and works offline
    import zhconv
except ImportError:  # pragma: no cover
    zhconv = None  # type: ignore[assignment]

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".ts",
    ".m2ts",
    ".avi",
    ".mov",
    ".wmv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

_RESOLUTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("2160p", re.compile(r"(?i)(?<![0-9])(?:2160p|4k|uhd)(?![0-9])")),
    ("1080p", re.compile(r"(?i)(?<![0-9])(?:1080p|1080i|fhd)(?![0-9])")),
    ("720p", re.compile(r"(?i)(?<![0-9])720p(?![0-9])")),
    ("480p", re.compile(r"(?i)(?<![0-9])480p(?![0-9])")),
)

_CODEC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("h265", re.compile(r"(?i)(?:x265|h[.\s]?265|hevc)")),
    ("h264", re.compile(r"(?i)(?:x264|h[.\s]?264|avc)")),
    ("av1", re.compile(r"(?i)(?<![a-z])av1(?![0-9])")),
)

_SEASON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(?<![A-Za-z0-9])S(\d{1,2})(?!\d)"),
    re.compile(r"(?i)\bSeason\s*(\d{1,2})\b"),
    re.compile(r"第\s*([0-9一二三四五六七八九十两]+)\s*季"),
)

_SXXEXX = re.compile(
    r"(?i)(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,4})(?:-?E?(\d{1,4}))?(?![\dPpIi])"
)
_EP = re.compile(r"(?i)(?<![A-Za-z0-9])E(?:P)?\s*(\d{1,4})(?![\dPpIi])")
_EP_CONT = re.compile(r"(?i)[-.]?E(\d{1,4})(?![\dPpIi])")
_EP_RANGE = re.compile(
    r"(?i)(?<![A-Za-z0-9])E(\d{1,3})\s*[-~至]\s*E?(\d{1,3})(?![\dPpIi])"
)
_CN_EP = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")
_CN_EP_RANGE = re.compile(r"第?\s*(\d{1,4})\s*[-~至]\s*(\d{1,4})\s*[集话話]")
# "更新至14集" is a cumulative pack of episodes 1..14, not a single episode.
_UPDATE_TO = re.compile(r"(?:更新至|已更新至)\s*第?\s*(\d{1,4})\s*[集话話期]")
_BATCH_TOTAL = re.compile(r"全\s*(\d{1,4})\s*[集话話]")
_BATCH_MARK = re.compile(r"(?i)合集|全集|complete|batch")

_YEAR = re.compile(r"(?<!\d)(19\d{2}|20[0-2]\d)(?!\d)")
_YEAR_EXCLUDE = {1080, 2160, 720, 480, 576, 264, 265}

_CHINESE_SUB = re.compile(
    r"(?i)中字|简中|繁中|简体|繁体|中英|双语|内封|内嵌|chs|cht|字幕"
)
_BIT_DEPTH = re.compile(
    r"(?i)(?<![0-9])(8|10|12)\s*[-_. ]?\s*bits?(?![a-z0-9])"
)
_HI10P = re.compile(r"(?i)hi[\s._-]?10p")
# Chinese dub / Mandarin audio track.  "国语中字" is an audio+subtitle marker;
# "中字" alone only means subtitles and must not count.
_CHINESE_AUDIO = re.compile(
    r"(?i)国语|国配|中配|中文配音|普通话|台配|港配|国粤|mandarin"
)
_LEADING_GROUP = re.compile(r"^\s*[\[【]([^\]】]{1,40})[\]】]\s*")
_TRAILING_GROUP = re.compile(r"[-@]([A-Za-z0-9_-]{2,30})$")
_GROUP_BLOCKLIST = {
    "dl",
    "web",
    "webdl",
    "hdr",
    "hdr10",
    "dv",
    "uhd",
    "remux",
    "bluray",
    "bdrip",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "avc",
    "aac",
    "dts",
    "truehd",
}

_CN_NUMERALS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_MAX_EPISODES = 200


@dataclass(frozen=True, slots=True)
class ParsedRelease:
    raw: str
    title_hint: str
    year: int | None
    season: int | None
    episodes: tuple[int, ...]
    is_batch: bool
    resolution: str | None
    video_codec: str | None
    bit_depth: int | None
    has_chinese_sub: bool
    group: str | None


def cn_to_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_NUMERALS.get(left, 1) if left else 1
        ones = _CN_NUMERALS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_NUMERALS.get(text)
    return None


def _normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    if zhconv is not None:
        text = zhconv.convert(text, "zh-cn")
    text = _LEADING_GROUP.sub("", text)
    text = text.lower()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def normalize_title(text: str) -> str:
    return _normalize_for_compare(text)


_SEASON_SUFFIX = re.compile(
    r"(?:[\s\-_.·:：]*)(?:第\s*[0-9一二三四五六七八九十百零〇两]+\s*季"
    r"|season\s*[0-9]{1,2}|s[0-9]{1,2})\s*$",
    re.IGNORECASE,
)


def season_from_title(title: str) -> int | None:
    """Read a season number such as 第二季 / Season 2 / S02 from a title."""
    text = str(title or "").strip()
    if not text:
        return None
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        number = cn_to_int(match.group(1))
        if number is not None and 0 < number <= 100:
            return number
    return None


def title_variants(title: str) -> list[str]:
    """Return the title plus a season-suffix-free base form.

    Douban often stores one subject per season ("老友记 第二季",
    "Friends Season 2"), while PT releases use the bare show name plus an
    Sxx marker.  Searching and matching both benefit from the base title.
    """
    text = str(title or "").strip()
    if not text:
        return []
    variants = [text]
    base = _SEASON_SUFFIX.sub("", text).strip(" -_.·:：")
    if base and base != text and len(_normalize_for_compare(base)) >= 2:
        variants.append(base)
    return variants


def _episodes_from_range(start: int, end: int) -> tuple[int, ...]:
    if start <= 0 or end < start:
        return ()
    end = min(end, start + _MAX_EPISODES - 1)
    return tuple(range(start, end + 1))


def _extract_episodes(text: str) -> tuple[tuple[int, ...], int | None, bool]:
    """Return (episodes, season_from_marker, batch_hint)."""
    season: int | None = None
    episodes: tuple[int, ...] = ()
    batch = False

    match = _SXXEXX.search(text)
    if match:
        season = int(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else start
        episodes = _episodes_from_range(start, end)

        # Support chains such as S01E01E02E03 without letting resolutions in.
        extras: list[int] = []
        position = match.end()
        while len(extras) < 20:
            continued = _EP_CONT.match(text, position)
            if not continued:
                break
            extras.append(int(continued.group(1)))
            position = continued.end()
        if extras:
            episodes = tuple(dict.fromkeys([*episodes, *extras]))

    if not episodes:
        match = _EP_RANGE.search(text)
        if match:
            episodes = _episodes_from_range(int(match.group(1)), int(match.group(2)))

    if not episodes:
        match = _CN_EP_RANGE.search(text)
        if match:
            episodes = _episodes_from_range(int(match.group(1)), int(match.group(2)))

    if not episodes:
        # Airing-show packs are named "Show S01 更新至14集": cumulative, so
        # they cover everything from episode 1 through the stated episode.
        match = _UPDATE_TO.search(text)
        if match:
            count = int(match.group(1))
            episodes = _episodes_from_range(1, count)
            batch = True

    if not episodes:
        match = _CN_EP.search(text)
        if match:
            episodes = (int(match.group(1)),)

    if not episodes:
        match = _EP.search(text)
        if match:
            episodes = (int(match.group(1)),)

    total = _BATCH_TOTAL.search(text)
    if total:
        count = int(total.group(1))
        batch = True
        if not episodes and 0 < count <= _MAX_EPISODES:
            episodes = _episodes_from_range(1, count)

    if _BATCH_MARK.search(text):
        batch = True
        if len(episodes) > 1:
            batch = True

    if season is None:
        for pattern in _SEASON_PATTERNS:
            found = pattern.search(text)
            if found:
                season = cn_to_int(found.group(1))
                if season is not None:
                    break

    return episodes, season, batch


def _extract_year(text: str) -> int | None:
    for match in _YEAR.finditer(text):
        value = int(match.group(1))
        if value not in _YEAR_EXCLUDE:
            return value
    return None


def _extract_resolution(text: str) -> str | None:
    for label, pattern in _RESOLUTION_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _extract_codec(text: str) -> str | None:
    for label, pattern in _CODEC_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _extract_bit_depth(text: str) -> int | None:
    if _HI10P.search(text):
        return 10
    match = _BIT_DEPTH.search(text)
    return int(match.group(1)) if match else None


def has_chinese_audio(*texts: str) -> bool:
    """Whether a release advertises a Chinese dub / Mandarin audio track.

    Only audio markers count: 中字/简中/繁中 describe subtitles and must not
    trigger the preference for a Chinese dub.
    """
    return any(_CHINESE_AUDIO.search(str(text or "")) for text in texts)


def _extract_group(text: str) -> str | None:
    leading = _LEADING_GROUP.search(text)
    if leading:
        return leading.group(1).strip()
    trailing = _TRAILING_GROUP.search(text)
    if trailing:
        candidate = trailing.group(1).strip()
        if (
            candidate
            and not candidate.isdigit()
            and not re.fullmatch(r"\d{3,4}[pi]?", candidate, flags=re.IGNORECASE)
            and candidate.lower() not in _GROUP_BLOCKLIST
        ):
            return candidate
    at_group = re.search(r"@([A-Za-z0-9_.-]{2,30})", text)
    if at_group:
        return at_group.group(1)
    return None


def _has_chinese_sub(text: str) -> bool:
    cleaned = text.replace("字幕组", "").replace("無字幕", "").replace("无字幕", "")
    return bool(_CHINESE_SUB.search(cleaned))


def _title_hint(text: str) -> str:
    stripped = _LEADING_GROUP.sub("", text).strip()
    stripped = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", stripped)
    cut_at = len(stripped)
    for pattern in (
        _SXXEXX,
        _EP_RANGE,
        _EP,
        _CN_EP_RANGE,
        _UPDATE_TO,
        _CN_EP,
        _YEAR,
        _BATCH_TOTAL,
        _BATCH_MARK,
        *_SEASON_PATTERNS,
    ):
        found = pattern.search(stripped)
        if found and found.start() < cut_at:
            cut_at = found.start()
    for pattern in (re.compile(r"(?i)(?:2160p|1080p|1080i|720p|480p|4k|uhd)"),):
        found = pattern.search(stripped)
        if found and found.start() < cut_at:
            cut_at = found.start()
    hint = stripped[:cut_at].strip(" .-_[]()【】")
    return hint or stripped


def parse_release(name: str) -> ParsedRelease:
    raw = str(name or "").strip()
    episodes, season, batch = _extract_episodes(raw)
    return ParsedRelease(
        raw=raw,
        title_hint=_title_hint(raw),
        year=_extract_year(raw),
        season=season,
        episodes=episodes,
        is_batch=batch,
        resolution=_extract_resolution(raw),
        video_codec=_extract_codec(raw),
        bit_depth=_extract_bit_depth(raw),
        has_chinese_sub=_has_chinese_sub(raw),
        group=_extract_group(raw),
    )


_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _title_boundary_ok(adjacent: str, edge: str) -> bool:
    """Whether an adjacent character ends the title rather than continuing it.

    Normalising strips punctuation, so "老友记" and "古宅老友记" become
    substrings of each other.  CJK has no word separators, which means a bare
    containment test would accept a completely different show; require the
    match to start/end at a non-CJK boundary instead.
    """
    if not adjacent:
        return True
    if _CJK_CHAR.match(edge):
        return not _CJK_CHAR.match(adjacent)
    return not adjacent.isalnum()


def _contains_title(haystack: str, needle: str) -> bool:
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index > 0 else ""
        after = (
            haystack[index + len(needle)]
            if index + len(needle) < len(haystack)
            else ""
        )
        if _title_boundary_ok(before, needle[0]) and _title_boundary_ok(
            after, needle[-1]
        ):
            return True
        start = index + 1


def title_similarity(sub_title: str, aliases: list[str], release_title: str) -> float:
    parsed = parse_release(release_title)
    haystacks = [
        _normalize_for_compare(release_title),
        _normalize_for_compare(parsed.title_hint),
    ]
    haystacks = [item for item in haystacks if item]
    best = 0.0
    for candidate in [sub_title, *aliases]:
        needle = _normalize_for_compare(candidate)
        if len(needle) < 2:
            continue
        for haystack in haystacks:
            if haystack == needle or _contains_title(haystack, needle):
                return 1.0
            if needle in haystack:
                # Matched only inside a longer unseparated name: a different
                # show ("老友记" vs "古宅老友记").  Do not let the fuzzy ratio
                # rescue it either.
                continue
            best = max(best, SequenceMatcher(None, needle, haystack).ratio())
    return best


def extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def is_video_file(path: str) -> bool:
    return extension(path) in VIDEO_EXTENSIONS


def is_subtitle_file(path: str) -> bool:
    return extension(path) in SUBTITLE_EXTENSIONS
