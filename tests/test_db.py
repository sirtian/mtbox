from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import Database


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.init()
    return db


class FlakyConnection:
    def __init__(self, failures: int, message: str = "database is locked") -> None:
        self.failures = failures
        self.message = message
        self.calls: list[str] = []

    def execute(self, sql: str) -> None:
        self.calls.append(sql)
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError(self.message)


def test_tx_commits(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    with db.tx() as conn:
        conn.execute("INSERT INTO setting(key, value) VALUES ('a', 'b')")
    with db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = 'a'").fetchone()
    assert row["value"] == "b"


def test_tx_rolls_back_pending_changes(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    with pytest.raises(RuntimeError, match="fail"):
        with db.tx() as conn:
            conn.execute("INSERT INTO setting(key, value) VALUES ('x', 'y')")
            raise RuntimeError("fail")
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM setting WHERE key = 'x'").fetchone()[0] == 0


def test_tx_does_not_mask_original_error_when_transaction_already_ended(
    tmp_path: Path,
) -> None:
    # SQLite 可能在错误后自动回滚；此时再 ROLLBACK 会报
    # “cannot rollback - no transaction is active”，不能掩盖原始异常。
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match="boom"):
        with db.tx() as conn:
            conn.execute("ROLLBACK")
            raise ValueError("boom")


def test_execute_retry_recovers_from_transient_lock() -> None:
    conn = FlakyConnection(failures=2)
    Database._execute_retry(conn, "BEGIN IMMEDIATE", attempts=3, delay=0)
    assert conn.calls == ["BEGIN IMMEDIATE"] * 3


def test_execute_retry_gives_up_after_attempts() -> None:
    conn = FlakyConnection(failures=5)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        Database._execute_retry(conn, "BEGIN IMMEDIATE", attempts=2, delay=0)
    assert len(conn.calls) == 2


def test_execute_retry_does_not_retry_other_errors() -> None:
    conn = FlakyConnection(failures=1, message="no such table: missing")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        Database._execute_retry(conn, "BEGIN IMMEDIATE", attempts=3, delay=0)
    assert len(conn.calls) == 1
