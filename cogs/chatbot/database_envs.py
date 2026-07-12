from logging import getLogger

from sqlalchemy import Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .database import ChatbotBase

logger = getLogger(__name__)


class DatabaseEnvs(ChatbotBase):
    __tablename__ = "database_envs"

    key = mapped_column(Text, primary_key=True)
    value = mapped_column(Text)

    def __repr__(self) -> str:
        """デバッグ用に設定キーと値を返します。"""
        return f"DatabaseEnvs(key={self.key!r}, value={self.value!r})"


class DatabaseEnvManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_env(self, key: str) -> str | None:
        """指定したキーに対応する環境変数の値をデータベースから取得します。

        Args:
            key: 取得したい環境変数のキー

        Returns:
            指定したキーに対応する値。存在しない場合は None。

        """
        async with self._session_factory() as session:
            result = await session.execute(select(DatabaseEnvs.value).where(DatabaseEnvs.key == key))
            return result.scalar_one_or_none()

    async def set_env(self, key: str, value: str) -> None:
        """指定したキーと値のペアをデータベースに保存します。

        既に同じキーが存在する場合は値を更新し、
        存在しない場合は新規に追加します。

        Args:
            key: 保存したい環境変数のキー
            value: 保存したい環境変数の値

        """
        async with self._session_factory.begin() as session:
            # 既に存在するかチェック
            result = await session.execute(select(DatabaseEnvs).where(DatabaseEnvs.key == key))

            if result.first():
                # UPDATE
                await session.execute(update(DatabaseEnvs).where(DatabaseEnvs.key == key).values(value=value))
            else:
                # INSERT
                await session.execute(insert(DatabaseEnvs).values(key=key, value=value))
