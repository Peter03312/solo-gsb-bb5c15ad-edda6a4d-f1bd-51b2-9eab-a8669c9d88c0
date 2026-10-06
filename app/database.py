"""SQLite 连接管理、建表脚本与读写事务辅助。

并发模型：
- FastAPI 在线程池中执行同步路由；每个请求使用独立连接（线程局部）。
- 所有写操作经由同一把进程内可重入锁串行化，配合读事务，保证
  “并发发布只准一个成功、失败不留部分报告”。
- 库文件开启 WAL + foreign_keys；写事务用 BEGIN IMMEDIATE 立即拿写锁。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import settings

# 串行化全部写事务（发布、读数、幂等表写入等），跨线程使用 RLock。
write_lock = threading.RLock()

_tls = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS indicators (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partitions (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandatory_indicators (
    partition_code TEXT NOT NULL REFERENCES partitions(code),
    indicator_code TEXT NOT NULL REFERENCES indicators(code),
    created_at     TEXT NOT NULL,
    PRIMARY KEY (partition_code, indicator_code)
);

-- 限值生效区间：左闭右开 [valid_from, valid_to)
CREATE TABLE IF NOT EXISTS limits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    partition_code TEXT NOT NULL REFERENCES partitions(code),
    indicator_code TEXT NOT NULL REFERENCES indicators(code),
    min_value      REAL,
    max_value      REAL,
    valid_from     TEXT NOT NULL,          -- UTC ISO8601 串（展示用）
    valid_to       TEXT NOT NULL,          -- UTC ISO8601 串（展示用）
    valid_from_ms  INTEGER NOT NULL,       -- 毫秒纪元，比较/重叠判定用
    valid_to_ms    INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (partition_code, indicator_code, valid_from_ms)
);

CREATE TABLE IF NOT EXISTS samples (
    id            TEXT PRIMARY KEY,
    partition_code TEXT NOT NULL REFERENCES partitions(code),
    sampled_at    TEXT NOT NULL,           -- UTC ISO8601 串
    sampled_at_ms INTEGER NOT NULL,
    -- 读数版本：登记后为 0，每次成功写入/更正读数 +1；发布不改版本
    reading_version INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    sample_id    TEXT NOT NULL REFERENCES samples(id),
    indicator_code TEXT NOT NULL REFERENCES indicators(code),
    value        REAL NOT NULL CHECK (value >= 0),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (sample_id, indicator_code)
);

-- 更正幂等：请求号唯一；同号重放返回原结果，同号异内容拒绝
CREATE TABLE IF NOT EXISTS correction_requests (
    request_no   TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 发布报告：同一 (sample_id, reading_version) 只能存在一条
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL REFERENCES samples(id),
    reading_version INTEGER NOT NULL,
    conclusion      TEXT NOT NULL,          -- qualified | unqualified
    snapshot_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (sample_id, reading_version)
);

CREATE INDEX IF NOT EXISTS idx_limits_lookup
    ON limits (partition_code, indicator_code, valid_from_ms, valid_to_ms);
CREATE INDEX IF NOT EXISTS idx_reports_sample ON reports (sample_id, id);
CREATE INDEX IF NOT EXISTS idx_samples_partition ON samples (partition_code);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30.0,
        isolation_level=None,  # 手工控制事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    """返回当前线程的连接（懒初始化，随线程复用）。"""
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = _connect()
        _tls.conn = conn
    return conn


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """只读事务快照。"""
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """写事务：进程锁串行化 + BEGIN IMMEDIATE，整体提交或整体回滚。"""
    conn = get_conn()
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """建表（幂等）。"""
    conn = get_conn()
    conn.executescript(SCHEMA)
