from __future__ import annotations

import sqlite3

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL CHECK (kind IN ('movie','tv')),
  title           TEXT NOT NULL,
  original_title  TEXT,
  aliases         TEXT NOT NULL DEFAULT '[]',
  year            INTEGER,
  douban_id       TEXT,
  imdb_id         TEXT,
  season          INTEGER NOT NULL DEFAULT 1,
  episode_offset  INTEGER NOT NULL DEFAULT 0,
  episode_from    INTEGER,
  episode_to      INTEGER,
  quality         TEXT NOT NULL DEFAULT '{}',
  rules           TEXT NOT NULL DEFAULT '{}',
  auto_grab       INTEGER NOT NULL DEFAULT 0,
  interval_min    INTEGER NOT NULL DEFAULT 30,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','paused','done')),
  deleted_at      TEXT,
  last_checked_at TEXT,
  last_error      TEXT,
  error_count     INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_due
  ON subscription(status, last_checked_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sub_kind ON subscription(kind);

CREATE TABLE IF NOT EXISTS torrent (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  mt_id           TEXT NOT NULL UNIQUE,
  subscription_id INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  size_bytes      INTEGER NOT NULL DEFAULT 0,
  discount        TEXT,
  seeders         INTEGER NOT NULL DEFAULT 0,
  leechers        INTEGER NOT NULL DEFAULT 0,
  category        TEXT,
  publish_at      TEXT,
  season          INTEGER,
  episode_start   INTEGER,
  episode_end     INTEGER,
  is_batch        INTEGER NOT NULL DEFAULT 0,
  score           REAL,
  reasons         TEXT NOT NULL DEFAULT '[]',
  state           TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (state IN ('candidate','grabbed','downloading','completed',
                                   'organized','organize_failed','ignored','failed','deleted')),
  ignored_by      TEXT,
  qb_hash         TEXT,
  save_path       TEXT,
  files           TEXT NOT NULL DEFAULT '[]',
  progress        REAL NOT NULL DEFAULT 0,
  grabbed_at      TEXT,
  completed_at    TEXT,
  organized_at    TEXT,
  seed_started_at TEXT,
  deleted_at      TEXT,
  error           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_torrent_sub_state ON torrent(subscription_id, state);
CREATE INDEX IF NOT EXISTS idx_torrent_hash ON torrent(qb_hash);
CREATE INDEX IF NOT EXISTS idx_torrent_created ON torrent(created_at);

CREATE TABLE IF NOT EXISTS episode_state (
  subscription_id INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
  season          INTEGER NOT NULL,
  episode         INTEGER NOT NULL,
  torrent_id      INTEGER,
  state           TEXT NOT NULL
                  CHECK (state IN ('grabbed','downloading','completed','organized')),
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (subscription_id, season, episode)
);

CREATE TABLE IF NOT EXISTS api_call (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint  TEXT NOT NULL,
  called_at TEXT NOT NULL,
  ok        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_api_call ON api_call(endpoint, called_at);

CREATE TABLE IF NOT EXISTS account_snapshot (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ratio       REAL,
  uploaded    INTEGER,
  downloaded  INTEGER,
  bonus       REAL,
  captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_run (
  name             TEXT PRIMARY KEY,
  last_started_at  TEXT,
  last_finished_at TEXT,
  ok               INTEGER,
  error            TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  level      TEXT NOT NULL,
  source     TEXT NOT NULL,
  message    TEXT NOT NULL,
  payload    TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_time ON event_log(created_at);

CREATE TABLE IF NOT EXISTS setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SCHEMA_V2 = """
ALTER TABLE subscription ADD COLUMN total_episodes INTEGER;
ALTER TABLE subscription ADD COLUMN available_episode INTEGER;
ALTER TABLE subscription ADD COLUMN episodes_source TEXT;
"""

SCHEMA_V3 = """
ALTER TABLE subscription ADD COLUMN library_category TEXT;
"""

SCHEMA_V4 = """
ALTER TABLE torrent ADD COLUMN content_path TEXT;
"""

SCHEMA_V5 = """
ALTER TABLE torrent ADD COLUMN organized_paths TEXT NOT NULL DEFAULT '[]';
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
    (4, SCHEMA_V4),
    (5, SCHEMA_V5),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def apply(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
