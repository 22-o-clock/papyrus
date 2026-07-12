import os
from logging import getLogger

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = getLogger(__name__)

_engine: AsyncEngine | None = None


class Base(DeclarativeBase):
    pass


def init_engine() -> async_sessionmaker[AsyncSession]:
    """Bot 起動時に1回だけ呼び出し、engine と sessionmaker を初期化して返す。"""
    global _engine

    _engine = create_async_engine(
        os.environ["SUPABASE_CONNECTION_STRING"],
        pool_size=5,
        max_overflow=2,
        pool_recycle=300,
    )
    logger.info("Database engine initialized")
    return async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_engine() -> None:
    """Bot 終了時に呼び出し、コネクションプールをクリーンアップする。"""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("Database engine disposed")


async def create_tables() -> None:
    """未作成のORMテーブルだけを作成します。"""
    if _engine is None:
        raise RuntimeError("Database engine is not initialized")
    async with _engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
