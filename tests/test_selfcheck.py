from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.errors import ConfigError
from app.services import selfcheck, settings_store


def make_dirs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "download_dir": tmp_path / "downloads",
        "library_dir": tmp_path / "media",
        "data_dir": tmp_path / "config",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "mt_api_key": "test-key",
        "path_map_json": '[[\"/downloads\", \"/downloads\"]]',
        "qb_save_path": "/downloads",
        **make_dirs(tmp_path),
    }
    values.update(overrides)
    return Settings(**values)


def make_db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.init()
    settings_store.ensure_defaults(database)
    return database


def run(tmp_path: Path, **overrides: object) -> list[selfcheck.Check]:
    settings = make_settings(tmp_path, **overrides)
    database = make_db(settings)
    return selfcheck.run_checks(settings, database, remote=False)


def levels(checks: list[selfcheck.Check]) -> dict[str, str]:
    return {check.name: check.level for check in checks}


def test_valid_configuration(tmp_path: Path) -> None:
    checks = run(tmp_path)
    assert not selfcheck.has_errors(checks)
    assert levels(checks)["MT API Key"] == selfcheck.LEVEL_OK
    assert levels(checks)["下载目录"] == selfcheck.LEVEL_OK
    assert levels(checks)["命名模板 movie_path_template"] == selfcheck.LEVEL_OK
    assert levels(checks)["命名模板 tv_path_template"] == selfcheck.LEVEL_OK


def test_missing_api_key_is_error(tmp_path: Path) -> None:
    checks = run(tmp_path, mt_api_key="")
    assert levels(checks)["MT API Key"] == selfcheck.LEVEL_ERROR
    with pytest.raises(ConfigError):
        selfcheck.require_valid(checks)


def test_missing_api_key_is_warning_for_web_startup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mt_api_key="")
    database = make_db(settings)
    checks = selfcheck.run_checks(
        settings, database, remote=False, credentials_required=False
    )
    assert levels(checks)["MT API Key"] == selfcheck.LEVEL_WARN
    selfcheck.require_valid(checks)


def test_missing_directory_is_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mt_api_key="test-key")
    missing = tmp_path / "nope"
    settings = settings.model_copy(update={"library_dir": missing})
    database = make_db(settings)
    checks = selfcheck.run_checks(settings, database, remote=False)
    assert levels(checks)["媒体库目录"] == selfcheck.LEVEL_ERROR


def test_invalid_path_map_is_error(tmp_path: Path) -> None:
    checks = run(tmp_path, path_map_json="not json")
    assert levels(checks)["路径映射"] == selfcheck.LEVEL_ERROR


def test_qb_save_path_must_map(tmp_path: Path) -> None:
    checks = run(
        tmp_path,
        path_map_json='[[\"/volume1/downloads\", \"/downloads\"]]',
        qb_save_path="/downloads",
    )
    assert levels(checks)["QB_SAVE_PATH 映射"] == selfcheck.LEVEL_ERROR


def test_unknown_template_placeholder_is_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = make_db(settings)
    settings_store.set_values(database, {"movie_path_template": "{library}/{bogus}.{ext}"})
    checks = selfcheck.run_checks(settings, database, remote=False)
    assert levels(checks)["命名模板 movie_path_template"] == selfcheck.LEVEL_ERROR
    with pytest.raises(ConfigError):
        selfcheck.require_valid(checks)


def test_category_template_placeholder_is_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = make_db(settings)
    settings_store.set_values(
        database,
        {
            "movie_path_template": "{library}/{category}/{title}.{ext}",
            "tv_path_template": "{library}/{category}/{title}/{title}.{ext}",
        },
    )
    checks = selfcheck.run_checks(settings, database, remote=False)
    assert levels(checks)["命名模板 movie_path_template"] == selfcheck.LEVEL_OK
    assert levels(checks)["命名模板 tv_path_template"] == selfcheck.LEVEL_OK


def test_template_without_title_is_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = make_db(settings)
    settings_store.set_values(database, {"tv_path_template": "{library}/{year}.{ext}"})
    checks = selfcheck.run_checks(settings, database, remote=False)
    assert levels(checks)["命名模板 tv_path_template"] == selfcheck.LEVEL_ERROR


def test_budget_plan_warns_when_subscriptions_too_dense(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = make_db(settings)
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO subscription(kind, title, interval_min, created_at, updated_at) "
            "VALUES ('tv', '示例', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
    checks = selfcheck.run_checks(settings, database, remote=False)
    assert levels(checks)["搜索预算规划"] == selfcheck.LEVEL_WARN


def test_settings_defaults_and_types(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = make_db(settings)
    settings_store.ensure_defaults(database)
    settings_store.ensure_defaults(database)

    assert settings_store.get_int(database, "default_interval_min") == 30
    assert settings_store.get_bool(database, "auto_grab_default") is False
    assert settings_store.get_json(database, "mode_map") == {"movie": "normal", "tv": "tvshow"}
    assert settings_store.get_json(database, "notify_config") == {}

    settings_store.set_values(database, {"min_score": "60"})
    assert settings_store.get_int(database, "min_score") == 60
