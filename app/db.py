from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import migrations

log = logging.getLogger("mtbox.db")

BUSY_RETRIES = 3
BUSY_RETRY_DELAY = 0.2
_BUSY_MARKERS = ("locked", "busy")


class Database:
    """Thin SQLite wrapper.

    One connection per unit of work: cheap with WAL and avoids sharing a
    connection between FastAPI's threadpool and APScheduler threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            self._execute_retry(conn, "BEGIN IMMEDIATE")
            try:
                yield conn
                if conn.in_transaction:
                    self._execute_retry(conn, "COMMIT")
            except BaseException:
                # 事务可能已被 SQLite 自动回滚（例如磁盘/IO 错误），
                # 此时再执行 ROLLBACK 会抛出 “cannot rollback - no transaction
                # is active” 并掩盖真正的错误，因此先检查再回滚。
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        log.warning("事务回滚失败，连接将直接关闭", exc_info=True)
                raise
        finally:
            conn.close()

    @staticmethod
    def _execute_retry(
        conn: sqlite3.Connection,
        sql: str,
        *,
        attempts: int = BUSY_RETRIES,
        delay: float = BUSY_RETRY_DELAY,
    ) -> None:
        """Retry BEGIN/COMMIT when another connection holds the write lock.

        ``busy_timeout`` already waits, but on NAS disks or while a backup is
        running the lock can outlast it; retrying briefly turns a transient
        lock into a slower write instead of a failed background job.
        """
        for attempt in range(attempts):
            try:
                conn.execute(sql)
                return
            except sqlite3.OperationalError as exc:
                if attempt + 1 >= attempts or not any(
                    marker in str(exc).lower() for marker in _BUSY_MARKERS
                ):
                    raise
                log.warning(
                    "数据库繁忙，%s 重试 %s/%s: %s", sql, attempt + 2, attempts, exc
                )
                time.sleep(delay * (2**attempt))

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            migrations.apply(conn)
        finally:
            conn.close()

    def vacuum_into(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()
