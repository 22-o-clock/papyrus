import json
from logging import getLogger

from sqlalchemy import Text, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase

logger = getLogger(__name__)


class DatabaseEnvironment(ChatbotBase):
    __tablename__ = "database_envs"

    key = mapped_column(Text, primary_key=True)
    value = mapped_column(Text)

    def __repr__(self) -> str:
        """デバッグ用に設定キーと値を返します。"""
        return f"DatabaseEnvironment(key={self.key!r}, value={self.value!r})"


class DatabaseEnvironmentRepository:
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
            result = await session.execute(select(DatabaseEnvironment.value).where(DatabaseEnvironment.key == key))
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
            result = await session.execute(select(DatabaseEnvironment).where(DatabaseEnvironment.key == key))

            if result.first():
                # UPDATE
                await session.execute(update(DatabaseEnvironment).where(DatabaseEnvironment.key == key).values(value=value))
            else:
                # INSERT
                await session.execute(insert(DatabaseEnvironment).values(key=key, value=value))

    async def update_json_mapping_entry(self, key: str, entry_key: str, value: str | None) -> None:
        """共有JSONオブジェクトの1要素をプロセス間で直列化して更新します。"""
        async with self._session_factory.begin() as session:
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
            current = await session.scalar(select(DatabaseEnvironment.value).where(DatabaseEnvironment.key == key))
            loaded = self._load_json_mapping(current)
            if value is None:
                loaded.pop(entry_key, None)
            else:
                loaded[entry_key] = value
            serialized = json.dumps(loaded, ensure_ascii=False, sort_keys=True)
            statement = insert(DatabaseEnvironment).values(key=key, value=serialized)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[DatabaseEnvironment.key],
                    set_={"value": serialized},
                )
            )

    async def update_json_string_set_member(self, key: str, member: str, *, enabled: bool) -> None:
        """共有JSON文字列集合の1要素をプロセス間で直列化して更新します。"""
        async with self._session_factory.begin() as session:
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
            current = await session.scalar(select(DatabaseEnvironment.value).where(DatabaseEnvironment.key == key))
            loaded = self._load_json_string_set(current)
            if enabled:
                loaded.add(member)
            else:
                loaded.discard(member)
            serialized = json.dumps(sorted(loaded))
            statement = insert(DatabaseEnvironment).values(key=key, value=serialized)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[DatabaseEnvironment.key],
                    set_={"value": serialized},
                )
            )

    @staticmethod
    def _load_json_mapping(value: str | None) -> dict[str, str]:
        """不正な既存値を空として扱い、文字列マッピングだけを返します。"""
        try:
            loaded = json.loads(value) if value is not None else {}
        except json.JSONDecodeError:
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            entry_key: entry_value
            for entry_key, entry_value in loaded.items()
            if isinstance(entry_key, str) and isinstance(entry_value, str)
        }

    @staticmethod
    def _load_json_string_set(value: str | None) -> set[str]:
        """不正な既存値を空として扱い、文字列要素だけを返します。"""
        try:
            loaded = json.loads(value) if value is not None else []
        except json.JSONDecodeError:
            return set()
        if not isinstance(loaded, list):
            return set()
        return {member for member in loaded if isinstance(member, str)}
