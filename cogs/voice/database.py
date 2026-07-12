import uuid

from sqlalchemy import Text, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from core.db import Base


class ArknightsVoiceTable(Base):
    """アークナイツのキャラクター名と台詞を保持する既存テーブル。"""

    __tablename__ = "arknights_prts"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ch_name = mapped_column(Text)
    en_name = mapped_column(Text)
    jp_name = mapped_column(Text, nullable=True)
    category = mapped_column(Text)
    phrase = mapped_column(Text)


class ArknightsVoiceDatabase:
    """アークナイツ台詞テーブルへのアクセスを提供する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_character_names(self) -> list[tuple[str, str, str | None]]:
        """キャラクター名の中国語・英語・日本語表記を取得する。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ArknightsVoiceTable.ch_name, ArknightsVoiceTable.en_name, ArknightsVoiceTable.jp_name).distinct(
                    ArknightsVoiceTable.ch_name
                )
            )
            return [
                (str(ch_name), str(en_name), str(jp_name) if jp_name is not None else None)
                for ch_name, en_name, jp_name in result.all()
            ]

    async def get_phrases(self, ch_name: str | None = None) -> list[tuple[str, str]]:
        """指定キャラクター、または全キャラクターの台詞を取得する。"""
        statement = select(ArknightsVoiceTable.category, ArknightsVoiceTable.phrase)
        if ch_name is not None:
            statement = statement.where(ArknightsVoiceTable.ch_name == ch_name)

        async with self._session_factory() as session:
            result = await session.execute(statement)
            return [(str(category), str(phrase)) for category, phrase in result.all()]

    async def find_phrases_containing(self, query: str, ch_name: str | None = None) -> list[str]:
        """指定した文字列を含む台詞を取得する。"""
        statement = select(ArknightsVoiceTable.phrase).where(ArknightsVoiceTable.phrase.like(f"%{query}%"))
        if ch_name is not None:
            statement = statement.where(ArknightsVoiceTable.ch_name == ch_name)

        async with self._session_factory() as session:
            result = await session.execute(statement)
            return [str(phrase) for phrase in result.scalars().all()]

    async def find_phrase(self, phrase: str) -> list[tuple[str, str]]:
        """完全一致する台詞のキャラクター名と種別を取得する。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ArknightsVoiceTable.ch_name, ArknightsVoiceTable.category).where(ArknightsVoiceTable.phrase == phrase)
            )
            return [(str(ch_name), str(category)) for ch_name, category in result.all()]
