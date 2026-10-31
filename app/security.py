"""Admin password verification, session CSRF tokens and login throttling."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request

from .config import Settings

CSRF_SESSION_KEY = "csrf_token"
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300


def admin_password_configured(settings: Settings) -> bool:
    return bool(settings.admin_password)


def verify_admin_password(settings: Settings, password: str) -> bool:
    if not settings.admin_password or not password:
        return False
    return hmac.compare_digest(password, settings.admin_password)


# ---------- CSRF ----------


def get_csrf_token(request: Request) -> str:
    session = request.session
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> bool:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token or not submitted:
        return False
    return hmac.compare_digest(token, submitted)


# ---------- login throttle ----------


@dataclass
class _Attempt:
    failures: int = 0
    locked_until: float = 0.0


class LoginThrottle:
    """In-process per-IP failure counter. A restart clears the lockout."""

    def __init__(
        self,
        max_failures: int = MAX_LOGIN_FAILURES,
        lockout_seconds: int = LOCKOUT_SECONDS,
    ) -> None:
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, _Attempt] = {}
        self._lock = threading.Lock()

    def locked_for(self, ip: str) -> int:
        now = time.monotonic()
        with self._lock:
            attempt = self._attempts.get(ip)
            if attempt is None:
                return 0
            remaining = int(attempt.locked_until - now)
            if remaining <= 0:
                if attempt.locked_until and attempt.locked_until <= now:
                    self._attempts.pop(ip, None)
                return 0
            return remaining

    def record_failure(self, ip: str) -> int:
        now = time.monotonic()
        with self._lock:
            attempt = self._attempts.setdefault(ip, _Attempt())
            attempt.failures += 1
            if attempt.failures >= self.max_failures:
                attempt.locked_until = now + self.lockout_seconds
                attempt.failures = 0
                return self.lockout_seconds
            return 0

    def reset(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)
