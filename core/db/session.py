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


class Base(DeclarativeBase):
    pass


class DatabaseRuntime:
    """Bot全体で1個のEngineとSession Factoryを管理する。"""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def initialize(self) -> async_sessionmaker[AsyncSession]:
        """未初期化なら接続プールを作成し、共有Session Factoryを返す。"""
        if self._session_factory is not None:
            return self._session_factory

        self._engine = create_async_engine(
            os.environ["SUPABASE_CONNECTION_STRING"],
            pool_size=1,
            max_overflow=0,
            pool_recycle=300,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        logger.info("Database engine initialized")
        return self._session_factory

    async def dispose(self) -> None:
        """共有Engineを破棄し、再初期化できる状態へ戻す。"""
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None
        logger.info("Database engine disposed")


DATABASE_RUNTIME = DatabaseRuntime()


def init_engine() -> async_sessionmaker[AsyncSession]:
    """Bot 起動時に1回だけ呼び出し、engine と sessionmaker を初期化して返す。"""
    return DATABASE_RUNTIME.initialize()


async def dispose_engine() -> None:
    """Bot 終了時に呼び出し、コネクションプールをクリーンアップする。"""
    await DATABASE_RUNTIME.dispose()
