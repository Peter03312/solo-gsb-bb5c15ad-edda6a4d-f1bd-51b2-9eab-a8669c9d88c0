"""SQLite 连接与模式初始化。

设计要点:
- 所有时间戳统一规范化为 UTC、微秒精度的 ISO 8601 字符串,
  格式固定(等宽、同一偏移后缀), 因此字典序比较等价于时间先后比较,
  限值生效区间的重叠/命中判断可直接用字符串比较完成。
- 连接使用 autocommit (isolation_level=None), 写路径显式
  `BEGIN IMMEDIATE` 取写锁, 保证"检查-写入"原子性, 并发发布只会有一个成功。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

-- 分区必测指标
CREATE TABLE IF NOT EXISTS required_indicators (
    zone        TEXT NOT NULL REFERENCES zones(name),
    indicator   TEXT NOT NULL,
    PRIMARY KEY (zone, indicator)
);

-- 指标限值的生效区间, 左闭右开 [effective_from, effective_to)
CREATE TABLE IF NOT EXISTS limit_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone            TEXT NOT NULL REFERENCES zones(name),
    indicator       TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT NOT NULL,
    max_value       REAL NOT NULL CHECK (max_value >= 0),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_limit_rules_zi ON limit_rules(zone, indicator);

-- 样本: version 为乐观锁, 每次读数录入/更正 +1
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    zone        TEXT NOT NULL REFERENCES zones(name),
    sampled_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- 当前读数(每样本每指标一条)
CREATE TABLE IF NOT EXISTS readings (
    sample_id   INTEGER NOT NULL REFERENCES samples(id),
    indicator   TEXT NOT NULL,
    value       REAL NOT NULL CHECK (value >= 0),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (sample_id, indicator)
);

-- 更正请求幂等表: request_id 全局唯一
CREATE TABLE IF NOT EXISTS correction_requests (
    request_id      TEXT PRIMARY KEY,
    sample_id       INTEGER NOT NULL,
    indicator       TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 发布报告(快照, 只增不改)
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    sample_version  INTEGER NOT NULL,
    report_version  INTEGER NOT NULL,
    zone            TEXT NOT NULL,
    sampled_at      TEXT NOT NULL,
    conclusion      TEXT NOT NULL CHECK (conclusion IN ('compliant', 'non_compliant')),
    created_at      TEXT NOT NULL,
    -- 同一读数版本不得重复发布
    UNIQUE (sample_id, sample_version),
    UNIQUE (sample_id, report_version)
);

-- 报告明细: 读数 + 限值依据快照
CREATE TABLE IF NOT EXISTS report_items (
    report_id       INTEGER NOT NULL REFERENCES reports(id),
    indicator       TEXT NOT NULL,
    value           REAL NOT NULL,
    limit_rule_id   INTEGER NOT NULL,
    max_value       REAL NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT NOT NULL,
    exceeded        INTEGER NOT NULL CHECK (exceeded IN (0, 1)),
    PRIMARY KEY (report_id, indicator)
);
"""


def utcnow() -> str:
    """当前 UTC 时间, 固定微秒精度 ISO 格式."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def norm_ts(dt: datetime) -> str:
    """把输入时间规范化为 UTC 固定格式字符串; 朴素时间按 UTC 处理."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
