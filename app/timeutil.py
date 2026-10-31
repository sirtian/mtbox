from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime(ISO_FORMAT)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_dt(value: object) -> datetime | None:
    """Parse ISO-8601 or the common 'YYYY-MM-DD HH:MM:SS' form (assumed UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in _FALLBACK_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hours_ago_iso(hours: float) -> str:
    return to_iso(utc_now() - timedelta(hours=hours))


def days_ago_iso(days: float) -> str:
    return to_iso(utc_now() - timedelta(days=days))


def add_minutes_iso(base: datetime, minutes: float) -> str:
    return to_iso(base + timedelta(minutes=minutes))


def local_display(value: object, tz_name: str = "Asia/Shanghai") -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return ""
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = timezone.utc
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")
