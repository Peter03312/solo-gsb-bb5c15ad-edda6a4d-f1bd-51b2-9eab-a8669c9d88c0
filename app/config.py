"""应用配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径（容器内默认 /data/waterworks.db，宿主机卷挂载持久化）
    db_path: str = os.environ.get("WATERWORKS_DB", "/data/waterworks.db")
    # 启动时是否自动初始化表结构并写入演示主数据（指标/分区/限值）
    seed_on_startup: bool = _get_bool("SEED_ON_STARTUP", True)
    # uvicorn 监听地址/端口（仅被 main.py 直接启动时使用）
    host: str = os.environ.get("APP_HOST", "0.0.0.0")
    port: int = int(os.environ.get("APP_PORT", "8080"))


settings = Settings()
