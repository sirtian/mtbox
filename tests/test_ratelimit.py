from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.errors import BudgetExceeded, CooldownActive, RateLimitBusy
from app.services import settings_store
from app.services.ratelimit import ApiLimiter


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init()
    return database


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "mt_api_key": "test-key",
        "mt_search_min_interval_sec": 0,
        "mt_search_daily_budget": 3,
        "mt_download_daily_budget": 2,
        "mt_profile_daily_budget": 5,
    }
    values.update(overrides)
    return Settings(**values)


def test_budget_exhaustion(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path), db)
    for _ in range(3):
        limiter.acquire("search")
    with pytest.raises(BudgetExceeded) as excinfo:
        limiter.acquire("search")
    assert excinfo.value.used == 3
    assert excinfo.value.budget == 3


def test_budget_is_persisted_across_instances(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = ApiLimiter(settings, db)
    first.acquire("torrent")
    first.acquire("torrent")

    second = ApiLimiter(settings, db)
    assert second.usage().torrent == 2
    with pytest.raises(BudgetExceeded):
        second.acquire("torrent")


def test_search_budget_reads_runtime_setting(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path, mt_search_daily_budget=3), db)
    assert limiter.budgets()["search"] == 3

    settings_store.set_values(db, {settings_store.SEARCH_DAILY_BUDGET_KEY: "5"})
    assert limiter.budgets()["search"] == 5
    for _ in range(5):
        limiter.acquire("search")
    with pytest.raises(BudgetExceeded) as excinfo:
        limiter.acquire("search")
    assert excinfo.value.budget == 5

    settings_store.delete_values(db, [settings_store.SEARCH_DAILY_BUDGET_KEY])
    assert limiter.budgets()["search"] == 3


def test_search_budget_setting_ignores_invalid_value(db: Database, tmp_path: Path) -> None:
    settings_store.set_values(db, {settings_store.SEARCH_DAILY_BUDGET_KEY: "not-a-number"})
    limiter = ApiLimiter(make_settings(tmp_path, mt_search_daily_budget=7), db)
    assert limiter.budgets()["search"] == 7


def test_search_interval_raises_when_not_waiting(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path, mt_search_min_interval_sec=30), db)
    limiter.acquire("search")
    with pytest.raises(RateLimitBusy) as excinfo:
        limiter.acquire("search", wait=False)
    assert excinfo.value.retry_after >= 1


def test_search_interval_is_enforced_with_wait(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(
        make_settings(tmp_path, mt_search_min_interval_sec=1, mt_search_daily_budget=10), db
    )
    limiter.acquire("search")
    started = time.monotonic()
    limiter.acquire("search", wait=True)
    assert time.monotonic() - started >= 0.9


def test_non_search_endpoints_ignore_search_interval(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path, mt_search_min_interval_sec=60), db)
    limiter.acquire("profile", wait=False)
    limiter.acquire("profile", wait=False)


def test_cooldown_blocks_and_clears(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path), db)
    limiter.set_cooldown(120, "Cloudflare")
    assert limiter.cooldown_remaining() > 0
    with pytest.raises(CooldownActive) as excinfo:
        limiter.acquire("profile")
    assert excinfo.value.reason == "Cloudflare"

    limiter.clear_cooldown()
    assert limiter.cooldown_remaining() == 0
    limiter.acquire("profile")


def test_record_updates_last_row(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path), db)
    limiter.acquire("search")
    limiter.record("search", False)
    with db.read() as conn:
        row = conn.execute("SELECT ok FROM api_call ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ok"] == 0


def test_usage_reports_all_endpoints(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path), db)
    limiter.acquire("search")
    limiter.acquire("torrent")
    limiter.acquire("profile")
    usage = limiter.usage()
    assert usage.as_dict() == {"search": 1, "torrent": 1, "profile": 1}


def test_unknown_endpoint_rejected(db: Database, tmp_path: Path) -> None:
    limiter = ApiLimiter(make_settings(tmp_path), db)
    with pytest.raises(ValueError):
        limiter.acquire("admin")
