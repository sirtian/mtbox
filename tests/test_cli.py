from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cli
from app.config import Settings


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    for name in ("downloads", "media", "config"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "mt_api_key": "test-key",
        "download_dir": tmp_path / "downloads",
        "library_dir": tmp_path / "media",
        "data_dir": tmp_path / "config",
        "path_map_json": '[[\"/downloads\", \"/downloads\"]]',
    }
    values.update(overrides)
    return Settings(**values)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from app import __version__

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert f"mtbox {__version__}" in capsys.readouterr().out


def test_pyproject_version_matches_package() -> None:
    import tomllib

    from app import __version__

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_check_config_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["check-config"]) == 0
    output = capsys.readouterr().out
    assert "配置校验通过" in output
    assert "[✓] MT API Key" in output


def test_check_config_invalid_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path, mt_api_key="")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["check-config"]) == 1
    captured = capsys.readouterr()
    assert "配置校验失败" in captured.err


def test_check_config_uses_saved_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.services import credentials
    from tests.helpers import make_db

    settings = make_settings(tmp_path, mt_api_key="")
    db = make_db(settings)
    credentials.save(db, credentials.MT_API_KEY, "stored-key")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["check-config"]) == 0
    assert settings.mt_api_key == "stored-key"
    assert "配置校验通过" in capsys.readouterr().out


def test_serve_uses_configured_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, port=18081)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    calls: dict[str, object] = {}
    fake_uvicorn = SimpleNamespace(run=lambda *a, **kw: calls.update(kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    assert cli.main(["serve"]) == 0
    assert calls["port"] == 18081
    assert calls["host"] == "0.0.0.0"


def test_backup_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["backup"]) == 0
    backups = list((tmp_path / "config" / "backups").glob("mtbox-*.db"))
    assert len(backups) == 1
    assert "已备份到" in capsys.readouterr().out


def test_cli_reports_expected_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path, mt_search_daily_budget=0)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    # the limiter rejects the call before any network request is made
    assert cli.main(["search", "x"]) == 2
    assert "错误" in capsys.readouterr().err
