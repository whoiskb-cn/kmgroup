# -*- coding: utf-8 -*-
"""
KMGroup 生产管理系统 - 数据库连接配置
"""

import os
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from routers.config import load_db_config

# 从统一配置文件读取配置
db_conf = load_db_config()
DB_HOST = db_conf.get("DB_HOST", "localhost")
DB_PORT = db_conf.get("DB_PORT", "5432")
DB_NAME = db_conf.get("DB_NAME", "kmgroup_db")
DB_USER = db_conf.get("DB_USER", "postgres")
DB_PASSWORD = db_conf.get("DB_PASSWORD", "")

# DATABASE_URL
DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=DB_USER,
    password=DB_PASSWORD or None,
    host=DB_HOST,
    port=int(DB_PORT or "5432"),
    database=DB_NAME,
)

# 异步引擎
_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
    "connect_args": {
        "server_settings": {
            "application_name": "kmgroup_erp",
            "jit": "off"
        }
    },
}
_POOL_SIZE_RAW = os.getenv("DB_POOL_SIZE")
if _POOL_SIZE_RAW:
    try:
        _engine_kwargs["pool_size"] = max(1, int(_POOL_SIZE_RAW))
    except (ValueError, TypeError):
        _engine_kwargs["pool_size"] = 10
else:
    _engine_kwargs["pool_size"] = 10

_MAX_OVERFLOW_RAW = os.getenv("DB_MAX_OVERFLOW")
if _MAX_OVERFLOW_RAW:
    try:
        _engine_kwargs["max_overflow"] = max(0, int(_MAX_OVERFLOW_RAW))
    except (ValueError, TypeError):
        _engine_kwargs["max_overflow"] = 20
else:
    _engine_kwargs["max_overflow"] = 20

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

# 会话工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# 基础模型
class Base(DeclarativeBase):
    pass

# 获取数据库 Session
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
