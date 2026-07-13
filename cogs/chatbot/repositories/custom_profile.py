import datetime
from dataclasses import dataclass

from sqlalchemy import BigInteger, Boolean, DateTime, Text, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .base import ChatbotBase


class ChatbotCustomProfile(ChatbotBase):
    """明示的なoption指定で使用する追加指示とモデル設定。"""

    __tablename__ = "custom_profiles"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False, default="system_default")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@dataclass(frozen=True)
class StoredCustomProfile:
    """回答生成層へ渡す有効なプロファイル定義。"""

    name: str
    instructions: str
    model: str
    enabled: bool


class CustomProfileRepository:
    """Papyrusのchatbotスキーマでプロファイルを管理するRepository。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, name: str, *, enabled_only: bool = False) -> StoredCustomProfile | None:
        """正規化済みの名前に対応するプロファイルを取得します。"""
        statement = select(ChatbotCustomProfile).where(ChatbotCustomProfile.name == name)
        if enabled_only:
            statement = statement.where(ChatbotCustomProfile.enabled.is_(True))
        async with self._session_factory() as session:
            profile = await session.scalar(statement)
        if profile is None:
            return None
        return StoredCustomProfile(
            name=profile.name,
            instructions=profile.instructions,
            model=profile.model,
            enabled=profile.enabled,
        )

    async def list_enabled(self) -> list[StoredCustomProfile]:
        """有効なプロファイルを名前順で返します。"""
        async with self._session_factory() as session:
            profiles = await session.scalars(
                select(ChatbotCustomProfile).where(ChatbotCustomProfile.enabled.is_(True)).order_by(ChatbotCustomProfile.name)
            )
            return [
                StoredCustomProfile(
                    name=profile.name,
                    instructions=profile.instructions,
                    model=profile.model,
                    enabled=profile.enabled,
                )
                for profile in profiles
            ]

    async def upsert(self, name: str, instructions: str, model: str, *, user_id: int) -> None:
        """プロファイルを作成し、同名の行があれば内容とモデルを更新して有効化します。"""
        statement = insert(ChatbotCustomProfile).values(
            name=name,
            instructions=instructions,
            model=model,
            enabled=True,
            created_by=user_id,
            updated_by=user_id,
        )
        async with self._session_factory.begin() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatbotCustomProfile.name],
                    set_={
                        "instructions": statement.excluded.instructions,
                        "model": statement.excluded.model,
                        "enabled": True,
                        "updated_by": user_id,
                        "updated_at": func.now(),
                    },
                )
            )

    async def disable(self, name: str, *, user_id: int) -> bool:
        """存在するプロファイルを無効化し、更新対象があればTrueを返します。"""
        async with self._session_factory.begin() as session:
            disabled_name = await session.scalar(
                update(ChatbotCustomProfile)
                .where(ChatbotCustomProfile.name == name)
                .values(enabled=False, updated_by=user_id, updated_at=func.now())
                .returning(ChatbotCustomProfile.name)
            )
        return disabled_name is not None
