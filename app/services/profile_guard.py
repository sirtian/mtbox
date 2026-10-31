"""Share-ratio guard backed by periodic ``/member/profile`` snapshots."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..db import Database
from ..errors import MTeamAuthError, MTeamError
from ..timeutil import parse_dt, to_iso, utc_now
from . import settings_store
from .audit import log_event, recent_event

log = logging.getLogger("mtbox.guard")

GUARD_KEY = "profile_guard"
MODES = {"ok", "free_only", "paused"}


@dataclass(frozen=True, slots=True)
class GuardState:
    mode: str = "ok"
    ratio: float | None = None
    updated_at: str | None = None

    @property
    def paused(self) -> bool:
        return self.mode == "paused"

    @property
    def free_only(self) -> bool:
        return self.mode == "free_only"

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "ratio": self.ratio, "updated_at": self.updated_at}


def load(db: Database) -> GuardState:
    raw = settings_store.get_value(db, GUARD_KEY)
    if not raw:
        return GuardState()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return GuardState()
    if not isinstance(data, dict):
        return GuardState()
    mode = str(data.get("mode") or "ok")
    if mode not in MODES:
        mode = "ok"
    ratio = data.get("ratio")
    try:
        ratio_value = float(ratio) if ratio is not None else None
    except (TypeError, ValueError):
        ratio_value = None
    return GuardState(
        mode=mode,
        ratio=ratio_value,
        updated_at=str(data["updated_at"]) if data.get("updated_at") else None,
    )


def save(db: Database, state: GuardState) -> None:
    settings_store.set_values(db, {GUARD_KEY: json.dumps(state.as_dict(), ensure_ascii=False)})


def evaluate_mode(ratio: float | None, pause_all: float, pause_nonfree: float) -> str:
    if ratio is None:
        return "ok"
    if ratio < pause_all:
        return "paused"
    if ratio < pause_nonfree:
        return "free_only"
    return "ok"


class ProfileGuard:
    def __init__(self, settings: Settings, db: Database, mt: Any, notifier: Any = None) -> None:
        self.settings = settings
        self.db = db
        self.mt = mt
        self.notifier = notifier

    def sync_snapshot(self, *, max_age_minutes: int = 15) -> GuardState:
        """Re-evaluate the guard from a fresh startup self-check snapshot.

        Avoids a second ``/member/profile`` call during startup. Stale snapshots
        are ignored so the persisted guard is not changed by old data.
        """
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT ratio, captured_at FROM account_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None or row["ratio"] is None:
            return load(self.db)
        captured = parse_dt(row["captured_at"])
        if captured is None or utc_now() - captured > timedelta(minutes=max_age_minutes):
            return load(self.db)
        ratio = float(row["ratio"])
        previous = load(self.db)
        mode = evaluate_mode(
            ratio,
            settings_store.get_float(self.db, "ratio_pause_all", 0.6),
            settings_store.get_float(self.db, "ratio_pause_nonfree", 0.8),
        )
        if mode == previous.mode and previous.ratio == ratio:
            return previous
        state = GuardState(mode=mode, ratio=ratio, updated_at=str(row["captured_at"]))
        save(self.db, state)
        if mode != previous.mode:
            self._announce(state, previous.mode)
        return state

    def refresh(self) -> GuardState:
        try:
            profile = self.mt.profile()
        except MTeamAuthError as exc:
            log.warning("刷新分享率失败: %s", exc)
            first_alert = not recent_event(
                self.db, source="guard", message="M-Team 认证失败"
            )
            log_event(self.db, "ERROR", "guard", "M-Team 认证失败", {"error": str(exc)})
            if first_alert:
                self._notify(
                    "mtbox API Key 失效",
                    "刷新账号信息时 M-Team 认证失败，请检查 MT_API_KEY。",
                    level="error",
                )
            return load(self.db)
        except MTeamError as exc:
            log.warning("刷新分享率失败: %s", exc)
            log_event(self.db, "WARN", "guard", "分享率刷新失败", {"error": str(exc)})
            return load(self.db)

        now = to_iso(utc_now())
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO account_snapshot(ratio, uploaded, downloaded, bonus, captured_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile.ratio, profile.uploaded, profile.downloaded, profile.bonus, now),
            )

        previous = load(self.db)
        if profile.ratio is None:
            # Parsing failure must not change the guard.
            log.info("分享率未解析到，保持原护栏状态 %s", previous.mode)
            return previous

        pause_all = settings_store.get_float(self.db, "ratio_pause_all", 0.6)
        pause_nonfree = settings_store.get_float(self.db, "ratio_pause_nonfree", 0.8)
        mode = evaluate_mode(profile.ratio, pause_all, pause_nonfree)
        state = GuardState(mode=mode, ratio=profile.ratio, updated_at=now)
        save(self.db, state)

        if mode != previous.mode:
            self._announce(state, previous.mode)
        return state

    def _announce(self, state: GuardState, previous_mode: str) -> None:
        if state.mode == "paused":
            message = f"分享率 {state.ratio:.2f} 低于阈值，暂停全部抓取"
        elif state.mode == "free_only":
            message = f"分享率 {state.ratio:.2f} 偏低，只允许免费种子"
        else:
            message = f"分享率 {state.ratio:.2f} 恢复，解除抓取限制"
        level = "ERROR" if state.mode == "paused" else "WARN" if state.mode == "free_only" else "INFO"
        log_event(
            self.db,
            level,
            "guard",
            message,
            {"mode": state.mode, "previous": previous_mode, "ratio": state.ratio},
        )
        self._notify(
            "mtbox 分享率护栏", message, level="warn" if level != "INFO" else "info"
        )

    def _notify(self, title: str, message: str, *, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(title, message, level=level)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.warning("发送通知失败", exc_info=True)
