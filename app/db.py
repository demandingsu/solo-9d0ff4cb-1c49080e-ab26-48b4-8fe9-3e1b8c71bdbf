"""SQLite 连接与事务管理。

并发与一致性设计：
- WAL 模式：读查询拿到一致的"已提交"快照，导入（写）不阻塞查询（读）；
- 全局写锁 + BEGIN IMMEDIATE：同一时刻只允许一个导入事务；
- 映射、流水、月度统计在同一个事务里提交 —— 查询端永远只看到
  三者同属一个已提交状态，不会看到"流水已进、统计未算"的中间态；
- 导入事务内任何失败都会 ROLLBACK，已有结果不变。
"""
from __future__ import annotations

import os
import sqlite3
import threading

_DEFAULT_DB_PATH = "/data/transit.db"

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id      TEXT PRIMARY KEY,
    file_type     TEXT NOT NULL CHECK (file_type IN ('swipes', 'mappings')),
    filename      TEXT NOT NULL,
    rows_received INTEGER NOT NULL,
    rows_applied  INTEGER NOT NULL,
    rows_skipped  INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
    txn_id          TEXT PRIMARY KEY,
    swipe_time_utc  TEXT NOT NULL,
    line            TEXT NOT NULL,
    temp_station_id TEXT NOT NULL,
    batch_id        TEXT NOT NULL REFERENCES import_batches (batch_id)
);
CREATE INDEX IF NOT EXISTS idx_swipes_lookup
    ON swipes (line, temp_station_id, swipe_time_utc);

CREATE TABLE IF NOT EXISTS mappings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    line            TEXT NOT NULL,
    temp_station_id TEXT NOT NULL,
    valid_from_utc  TEXT NOT NULL,
    valid_to_utc    TEXT NOT NULL,
    real_station    TEXT NOT NULL,
    batch_id        TEXT NOT NULL REFERENCES import_batches (batch_id),
    CHECK (valid_from_utc < valid_to_utc)
);
CREATE INDEX IF NOT EXISTS idx_mappings_lookup
    ON mappings (line, temp_station_id);

-- 物化统计：与导入同事务重算、同事务提交
CREATE TABLE IF NOT EXISTS monthly_stats (
    month        TEXT NOT NULL,   -- UTC 月份，格式 YYYY-MM
    real_station TEXT NOT NULL,
    boardings    INTEGER NOT NULL,
    PRIMARY KEY (month, real_station)
);
"""


def db_path() -> str:
    return os.environ.get("TRANSIT_DB_PATH", _DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path(), isolation_level=None, check_same_thread=False, timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


class write_txn:
    """序列化写事务：进入时取全局写锁并 BEGIN IMMEDIATE；
    正常退出 COMMIT，异常退出 ROLLBACK（导入失败不改变已有结果）。"""

    def __enter__(self) -> sqlite3.Connection:
        _write_lock.acquire()
        try:
            self._conn = connect()
            self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            _write_lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._conn.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        finally:
            self._conn.close()
            _write_lock.release()
        return False
