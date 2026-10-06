#!/usr/bin/env python3
"""手工初始化数据库：建表 + 幂等写入种子主数据。

用法（本地）：
    WATERWORKS_DB=./data/waterworks.db python scripts/init_db.py
容器内已在应用启动时自动执行初始化，通常无需手动运行本脚本。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.seed import initialize  # noqa: E402

if __name__ == "__main__":
    result = initialize()
    if result is None:
        print("schema initialized (SEED_ON_STARTUP=false, seed data skipped)")
    else:
        print(f"initialized: {result}")
