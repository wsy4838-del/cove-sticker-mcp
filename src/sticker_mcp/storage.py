from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  extension TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  ocr_text TEXT NOT NULL DEFAULT '',
  semantic_description TEXT NOT NULL DEFAULT '',
  emotions_json TEXT NOT NULL DEFAULT '[]',
  scenes_json TEXT NOT NULL DEFAULT '[]',
  keywords_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  manually_edited INTEGER NOT NULL DEFAULT 0,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_feedback TEXT,
  last_used_at TEXT,
  manual_fields_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_stickers_deleted ON stickers(deleted);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sticker_id TEXT NOT NULL,
  session_id TEXT,
  turn INTEGER,
  used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id, used_at);
CREATE TABLE IF NOT EXISTS tag_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sticker_id TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(stickers)")}
        if "manual_fields_json" not in columns:
            self.connection.execute("ALTER TABLE stickers ADD COLUMN manual_fields_json TEXT NOT NULL DEFAULT '[]'")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def transaction(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN")
        return self.connection

    def get_setting(self, key: str) -> Any:
        row = self.connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO settings(key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.connection.commit()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(sql, params)
