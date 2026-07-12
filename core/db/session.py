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

_engine: AsyncEngine | None = None


class Base(DeclarativeBase):
    pass


def init_engine() -> async_sessionmaker[AsyncSession]:
    """Bot 起動時に1回だけ呼び出し、engine と sessionmaker を初期化して返す。"""
    global _engine

    _engine, session_factory = create_session_factory(os.environ["SUPABASE_CONNECTION_STRING"])
    logger.info("Database engine initialized")
    return session_factory


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
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("Database engine disposed")


async def create_tables() -> None:
    """未作成のORMテーブルだけを作成します。"""
    if _engine is None:
        message = "Database engine is not initialized"
        raise RuntimeError(message)
    await create_tables_for(_engine, Base.metadata)


async def create_tables_for(engine: AsyncEngine, metadata: MetaData, *, schema: str | None = None) -> None:
    """指定エンジンに、必要に応じてスキーマとORMテーブルを作成します。"""
    async with engine.begin() as connection:
        if schema is not None:
            if not schema.isidentifier():
                message = "Schema name must be a valid identifier"
                raise ValueError(message)
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await connection.run_sync(metadata.create_all)
