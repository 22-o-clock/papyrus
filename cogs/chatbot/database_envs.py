import os
from logging import getLogger
from typing import Any

from sqlalchemy import CursorResult, Text, insert, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, DeclarativeMeta, mapped_column
from sqlalchemy.sql.expression import Select

logger = getLogger(__name__)

CONNECTION_STRING = os.environ["SUPABASE_CONNECTION_STRING"]


class Base(DeclarativeBase):
    pass


class DatabaseEnvs(Base):
    __tablename__ = "database_envs"

    key = mapped_column(Text, primary_key=True)
    value = mapped_column(Text)

    def __repr__(self) -> str:
        return f"DatabaseEnvs(key={self.key!r}, value={self.value!r})"


class DatabaseEnvManager:
    def __init__(self) -> None:
        self.engine: AsyncEngine = create_async_engine(CONNECTION_STRING)

    async def create_all_tables(self, model: DeclarativeMeta) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(model.metadata.create_all)

        await self.engine.dispose()

    async def drop_all_tables(self, model: DeclarativeMeta) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(model.metadata.drop_all)

        await self.engine.dispose()

    async def get_env(self, key: str) -> str | None:
        """指定したキーに対応する環境変数の値をデータベースから取得します。

        Args:
            key: 取得したい環境変数のキー

        Returns:
            指定したキーに対応する値。存在しない場合は None。

        """
        async with self.engine.connect() as conn:
            cursor_result: CursorResult[Any] = await conn.execute(Select(DatabaseEnvs.value).where(DatabaseEnvs.key == key))
            result = cursor_result.all()

        await self.engine.dispose()

        return result[0][0] if result else None

    async def set_env(self, key: str, value: str) -> None:
        """指定したキーと値のペアをデータベースに保存します。

        既に同じキーが存在する場合は値を更新し、
        存在しない場合は新規に追加します。

        Args:
            key: 保存したい環境変数のキー
            value: 保存したい環境変数の値

        """
        async with self.engine.begin() as conn:
            # 既に存在するかチェック
            result: CursorResult[Any] = await conn.execute(Select(DatabaseEnvs).where(DatabaseEnvs.key == key))

            if result.first():
                # UPDATE
                await conn.execute(update(DatabaseEnvs).where(DatabaseEnvs.key == key).values(value=value))
            else:
                # INSERT
                await conn.execute(insert(DatabaseEnvs).values(key=key, value=value))

        await self.engine.dispose()
