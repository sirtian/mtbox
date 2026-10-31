from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.security import (
    LoginThrottle,
    admin_password_configured,
    verify_admin_password,
)


def test_plaintext_admin_password(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, admin_password="legacy-secret")
    assert admin_password_configured(settings)
    assert verify_admin_password(settings, "legacy-secret")
    assert not verify_admin_password(settings, "other")
    assert not verify_admin_password(settings, "")


def test_no_admin_password_configured(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert not admin_password_configured(settings)
    assert not verify_admin_password(settings, "anything")


def test_login_throttle_locks_after_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.security.time.monotonic", lambda: clock["now"])
    throttle = LoginThrottle(max_failures=5, lockout_seconds=300)

    for _ in range(4):
        assert throttle.record_failure("1.2.3.4") == 0
    assert throttle.locked_for("1.2.3.4") == 0

    assert throttle.record_failure("1.2.3.4") == 300
    assert throttle.locked_for("1.2.3.4") == 300

    clock["now"] += 301
    assert throttle.locked_for("1.2.3.4") == 0


def test_login_throttle_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.security.time.monotonic", lambda: clock["now"])
    throttle = LoginThrottle(max_failures=2, lockout_seconds=60)

    throttle.record_failure("ip")
    throttle.record_failure("ip")
    assert throttle.locked_for("ip") > 0
    throttle.reset("ip")
    assert throttle.locked_for("ip") == 0
