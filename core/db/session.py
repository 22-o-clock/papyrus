import os
from logging import getLogger

from sqlalchemy import MetaData, text
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

    @property
    def engine(self) -> AsyncEngine:
        """初期化済みの共有Engineを返す。"""
        if self._engine is None:
            message = "Database engine is not initialized"
            raise RuntimeError(message)
        return self._engine

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


def create_session_factory(
    connection_string: str,
    *,
    search_path: str | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """指定した接続先用の非同期エンジンとセッションファクトリを作成します。"""
    engine_options: dict[str, object] = {
        "pool_size": 5,
        "max_overflow": 2,
        "pool_recycle": 300,
    }
    if search_path is not None:
        engine_options["connect_args"] = {"server_settings": {"search_path": search_path}}
    engine = create_async_engine(connection_string, **engine_options)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engine() -> None:
    """Bot 終了時に呼び出し、コネクションプールをクリーンアップする。"""
    await DATABASE_RUNTIME.dispose()


async def create_tables() -> None:
    """未作成のORMテーブルだけを作成します。"""
    await create_tables_for(DATABASE_RUNTIME.engine, Base.metadata)


async def create_tables_for(engine: AsyncEngine, metadata: MetaData, *, schema: str | None = None) -> None:
    """指定エンジンに、必要に応じてスキーマとORMテーブルを作成します。"""
    async with engine.begin() as connection:
        if schema is not None:
            if not schema.isidentifier():
                message = "Schema name must be a valid identifier"
                raise ValueError(message)
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await connection.run_sync(metadata.create_all)
