from sqlalchemy import BigInteger, Integer, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from core.db import Base


class VoiceVoxTable(Base):
    __tablename__ = "voicevox"

    member_id = mapped_column(BigInteger, primary_key=True)
    speaker_id = mapped_column(Integer)

    def __repr__(self) -> str:
        """デバッグ用にメンバーIDと話者IDを表現する。"""
        return f"VoiceVoxTable(member_id={self.member_id!r}, speaker_id={self.speaker_id!r})"


class VoiceVoxDatabase:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_speakers(self) -> dict[int, int]:
        """使用 speaker 情報の取得"""
        async with self._session_factory() as session:
            result = await session.execute(select(VoiceVoxTable.member_id, VoiceVoxTable.speaker_id))
        return {int(member_id): int(speaker_id) for member_id, speaker_id in result.tuples()}

    async def set_speaker(self, member_id: int, speaker_id: int) -> None:
        """使用 speaker 情報の保存"""
        async with self._session_factory.begin() as session:
            stmt = insert(VoiceVoxTable).values(member_id=member_id, speaker_id=speaker_id)
            stmt = stmt.on_conflict_do_update(
                index_elements=[VoiceVoxTable.member_id],
                set_={"speaker_id": speaker_id},
            )
            await session.execute(stmt)
