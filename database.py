# -*- coding: utf-8 -*-
"""
KMGroup 生产管理系统 - 数据库连接配置
"""

import os
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.routers.config import load_db_config

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
    port=int(DB_PORT),
    database=DB_NAME,
)

# 异步引擎
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

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
