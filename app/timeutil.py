"""时间工具：入参强制带时区，统一归一化为 UTC；输出 ISO8601 用 Z 结尾。"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware, e.g. 2025-06-01T08:00:00+08:00")
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    """UTC datetime -> '2025-06-01T00:00:00.123000Z'（微秒为 0 时省略小数部分）。"""
    dt = to_utc(dt)
    text = dt.isoformat()
    if dt.microsecond == 0:
        return text[:19] + "Z"
    return text.replace("+00:00", "Z")


def epoch_ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
