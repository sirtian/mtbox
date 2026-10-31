from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import migrations
from app.config import Settings
from app.db import Database
from app.errors import ConfigError
from app.main import create_app


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    for name in ("downloads", "media", "config"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "mt_api_key": "test-key",
        "download_dir": tmp_path / "downloads",
        "library_dir": tmp_path / "media",
        "data_dir": tmp_path / "config",
        "path_map_json": '[[\"/downloads\", \"/downloads\"]]',
        "qb_username": "",
        "session_secret": "test-secret",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_port_defaults_and_override(tmp_path: Path) -> None:
    assert make_settings(tmp_path).port == 8080
    assert make_settings(tmp_path, port=18080).port == 18080


def test_health_and_dashboard(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), run_remote_checks=False)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["db"] == "ok"
        assert payload["qb"]["ok"] is False
        assert payload["budget"] == {"search_24h": 0, "torrent_24h": 0}

        dashboard = client.get("/", follow_redirects=False)
        assert dashboard.status_code == 303
        assert dashboard.headers["location"].startswith("/login")

        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert "登录" in login_page.text


def test_startup_allows_missing_api_key(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, mt_api_key=""), run_remote_checks=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_startup_refuses_invalid_config(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(
        update={"library_dir": tmp_path / "missing"}
    )
    app = create_app(settings, run_remote_checks=False)
    with pytest.raises(ConfigError):
        with TestClient(app):
            pass


def test_migration_adds_library_category(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(migrations.SCHEMA_V1 + migrations.SCHEMA_V2)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    database = Database(path)
    database.init()

    with database.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscription)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "library_category" in columns
    assert version == migrations.LATEST_VERSION


def test_migration_adds_torrent_content_path(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(migrations.SCHEMA_V1 + migrations.SCHEMA_V2 + migrations.SCHEMA_V3)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    database = Database(path)
    database.init()

    with database.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(torrent)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "content_path" in columns
    assert version == migrations.LATEST_VERSION


def test_migration_adds_organized_paths(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            migrations.SCHEMA_V1
            + migrations.SCHEMA_V2
            + migrations.SCHEMA_V3
            + migrations.SCHEMA_V4
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()

    database = Database(path)
    database.init()

    with database.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(torrent)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "organized_paths" in columns
    assert version == migrations.LATEST_VERSION


def test_startup_migrates_database(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), run_remote_checks=False)
    with TestClient(app) as client:
        client.get("/health")
    db_path = tmp_path / "config" / "mtbox.db"
    assert db_path.exists()
    assert (tmp_path / "config" / "logs" / "mtbox.log").exists()
