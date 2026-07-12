import uuid
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from uuid import uuid4

from sqlalchemy import BigInteger, Text, delete, select
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import MappedColumn, mapped_column

from core.db import Base

logger: Logger = getLogger(__name__)
JST = timezone(timedelta(hours=9))


class ReminderTable(Base):
    __tablename__ = "reminder"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    author = mapped_column(BigInteger, nullable=False)
    channel = mapped_column(BigInteger, nullable=False)
    content = mapped_column(Text, nullable=False)
    time: MappedColumn[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """デバッグ用にリマインダーの全項目を表現する。"""
        return f"ReminderTable({self.id=}, {self.author=}, {self.channel=}, \
                {self.content=}, {self.time=})"

    def almost_equal(self, now: datetime) -> bool:
        return self.time.astimezone(JST).strftime("%Y-%m-%d %H:%M") == now.astimezone(JST).strftime("%Y-%m-%d %H:%M")

    def behind(self, now: datetime) -> bool:
        return self.time.astimezone(JST) < now.astimezone(JST)


class ReminderDatabase:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_all_reminders(self) -> list[ReminderTable]:
        """全てのリマインダーの取得"""
        async with self._session_factory() as session:
            result = await session.execute(select(ReminderTable))
            reminders = [
                ReminderTable(
                    id=rem.id,
                    author=rem.author,
                    channel=rem.channel,
                    content=rem.content,
                    time=rem.time.astimezone(JST),
                )
                for rem in result.scalars().all()
            ]
            return list(reminders)

    async def add_reminder(self, author_id: int, channel_id: int, content: str, time: datetime) -> None:
        """リマインダーの追加"""
        async with self._session_factory.begin() as session:
            reminder = ReminderTable(
                author=author_id,
                channel=channel_id,
                content=content,
                time=time,
            )
            session.add(reminder)
            await session.flush()

    async def get_reminder(self, reminder_id: uuid.UUID, author_id: int) -> ReminderTable | None:
        """指定利用者が所有するリマインダーをIDで取得する。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ReminderTable).where(ReminderTable.id == reminder_id, ReminderTable.author == author_id)
            )
            return result.scalar_one_or_none()

    async def remove(self, reminder_id: uuid.UUID) -> None:
        """リマインダーの削除"""
        async with self._session_factory.begin() as session:
            await session.execute(delete(ReminderTable).where(ReminderTable.id == reminder_id))
