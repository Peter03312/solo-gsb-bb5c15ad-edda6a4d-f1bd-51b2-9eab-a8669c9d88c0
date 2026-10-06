"""启动初始化（幂等）：建表并写入演示主数据。

SEED_ON_STARTUP=false 时仅建表，不写种子数据。
种子数据用固定时间锚点，示例采样时刻 2025-06-01 全部命中生效限值。
"""
from __future__ import annotations

import sqlite3

from . import crud, timeutil
from .database import get_conn, init_db, write_tx
from .schemas import LimitCreate, SampleCreate  # noqa: F401  (保留给手工脚本使用)


# (indicator_code, name, unit)
SEED_INDICATORS = [
    ("turbidity", "浑浊度", "NTU"),
    ("ph", "pH", ""),
    ("residual_chlorine", "游离余氯", "mg/L"),
]

# (partition_code, name)
SEED_PARTITIONS = [
    ("P1", "出厂水一号分区"),
    ("P2", "管网水二号分区"),
]

# 每个分区的必测指标
SEED_MANDATORY = {
    "P1": ["turbidity", "ph", "residual_chlorine"],
    "P2": ["turbidity", "ph", "residual_chlorine"],
}

# (partition, indicator, min, max, valid_from, valid_to) —— 左闭右开
SEED_LIMITS = [
    ("P1", "turbidity", None, 1.0, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("P1", "ph", 6.5, 8.5, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("P1", "residual_chlorine", None, 4.0, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("P2", "turbidity", None, 1.0, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("P2", "ph", 6.5, 8.5, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("P2", "residual_chlorine", 0.05, 4.0, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
]


def seed() -> dict[str, int]:
    """幂等写入种子主数据，返回各类新建条数。"""
    init_db()
    counts = {"indicators": 0, "partitions": 0, "mandatory": 0, "limits": 0}
    conn = get_conn()

    with write_tx() as c:
        for code, name, unit in SEED_INDICATORS:
            if not c.execute("SELECT 1 FROM indicators WHERE code=?", (code,)).fetchone():
                c.execute(
                    "INSERT INTO indicators(code, name, unit, created_at) VALUES (?,?,?,?)",
                    (code, name, unit, timeutil.iso_z(timeutil.utcnow())),
                )
                counts["indicators"] += 1
        for code, name in SEED_PARTITIONS:
            if not c.execute("SELECT 1 FROM partitions WHERE code=?", (code,)).fetchone():
                c.execute(
                    "INSERT INTO partitions(code, name, created_at) VALUES (?,?,?)",
                    (code, name, timeutil.iso_z(timeutil.utcnow())),
                )
                counts["partitions"] += 1
        for partition_code, codes in SEED_MANDATORY.items():
            for indicator_code in codes:
                if not c.execute(
                    "SELECT 1 FROM mandatory_indicators WHERE partition_code=? AND indicator_code=?",
                    (partition_code, indicator_code),
                ).fetchone():
                    c.execute(
                        "INSERT INTO mandatory_indicators(partition_code, indicator_code, created_at) VALUES (?,?,?)",
                        (partition_code, indicator_code, timeutil.iso_z(timeutil.utcnow())),
                    )
                    counts["mandatory"] += 1

    # 限值走业务逻辑（重叠/成员校验一致），逐条独立幂等
    for partition_code, indicator_code, min_v, max_v, vfrom, vto in SEED_LIMITS:
        with write_tx() as c:
            exists = c.execute(
                """SELECT 1 FROM limits WHERE partition_code=? AND indicator_code=? AND valid_from=?""",
                (partition_code, indicator_code, vfrom),
            ).fetchone()
        if exists:
            continue
        body = LimitCreate(
            indicator_code=indicator_code,
            min_value=min_v,
            max_value=max_v,
            valid_from=vfrom,
            valid_to=vto,
        )
        crud.create_limit(partition_code, body)
        counts["limits"] += 1

    return counts


def initialize() -> dict[str, int] | None:
    from .config import settings

    if not settings.seed_on_startup:
        init_db()
        return None
    return seed()
