import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, Table, Text, select
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

TALKDATA_SCHEMA = "talkdata"


@dataclass(frozen=True, slots=True)
class MessageLog:
    name: str
    channel_id: int
    content: str


class TalkDataNotFoundError(Exception):
    """TalkDataに必要なレコードがないことを表す。"""


class DiscordMember(Base):
    __tablename__ = "member"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_name: Mapped[str] = mapped_column(Text)


class DiscordChannel(Base):
    __tablename__ = "channel"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[int] = mapped_column(ForeignKey(f"{TALKDATA_SCHEMA}.channel.id"))


class DiscordMessage(Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    edit_count: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(f"{TALKDATA_SCHEMA}.channel.id"))
    member_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(f"{TALKDATA_SCHEMA}.member.id"))
    reply_id: Mapped[int] = mapped_column(BigInteger)
    reply_edit_count: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text)
    attachment: Mapped[str] = mapped_column(Text)
    post_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[int] = mapped_column(default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["reply_id", "reply_edit_count"],
            [f"{TALKDATA_SCHEMA}.message.id", f"{TALKDATA_SCHEMA}.message.edit_count"],
        ),
        {"schema": TALKDATA_SCHEMA},
    )


class TalkDataDatabase:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._database_schema = "talkdata_test" if os.getenv("DEBUG") == "True" else "talkdata"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """実行時設定に対応するスキーマへ接続したSessionを返す。"""
        async with self._session_factory() as session:
            await session.connection(execution_options={"schema_translate_map": {TALKDATA_SCHEMA: self._database_schema}})
            yield session

    async def initialize(self, now: datetime) -> None:
        """TalkData用テーブルと外部キー参照用のダミーレコードを準備する。"""
        async with self.session() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        cast("Table", DiscordMember.__table__),
                        cast("Table", DiscordChannel.__table__),
                        cast("Table", DiscordMessage.__table__),
                    ],
                )
            )
            if await session.get(DiscordMember, 0) is None:
                session.add(DiscordMember(id=0, display_name="dummy"))
            if await session.get(DiscordChannel, 0) is None:
                session.add(DiscordChannel(id=0, name="dummy", parent_id=0))
            if await session.get(DiscordMessage, (0, 0)) is None:
                session.add(
                    DiscordMessage(
                        id=0,
                        edit_count=0,
                        channel_id=0,
                        member_id=0,
                        reply_id=0,
                        reply_edit_count=0,
                        content="",
                        attachment="",
                        post_time=now,
                    )
                )
            await session.commit()

    async def connection_is_available(self) -> bool:
        try:
            async with self.session() as session:
                await session.scalar(select(DiscordMember.id).limit(1))
        except SQLAlchemyError:
            return False
        return True

    async def upsert_member(self, session: AsyncSession, member_id: int, display_name: str) -> None:
        """メンバーを追加し、既存IDなら表示名を更新する。"""
        statement = postgresql_insert(DiscordMember).values(id=member_id, display_name=display_name)
        statement = statement.on_conflict_do_update(
            index_elements=[DiscordMember.id],
            set_={"display_name": statement.excluded.display_name},
        )
        await session.execute(statement)

    async def upsert_channel(self, session: AsyncSession, channel_id: int, name: str, parent_id: int) -> None:
        """チャンネルを追加し、既存IDなら名称と親チャンネルを更新する。"""
        statement = postgresql_insert(DiscordChannel).values(id=channel_id, name=name, parent_id=parent_id)
        statement = statement.on_conflict_do_update(
            index_elements=[DiscordChannel.id],
            set_={"name": statement.excluded.name, "parent_id": statement.excluded.parent_id},
        )
        await session.execute(statement)

    async def get_message_log(self, session: AsyncSession, message_id: int, edit_count: int) -> MessageLog:
        row = (
            await session.execute(
                select(DiscordMember.display_name, DiscordChannel.id, DiscordMessage.content).where(
                    DiscordMessage.id == message_id,
                    DiscordMessage.edit_count == edit_count,
                    DiscordMember.id == DiscordMessage.member_id,
                    DiscordChannel.id == DiscordMessage.channel_id,
                )
            )
        ).one_or_none()
        if row is None:
            error_message = f"対応するメッセージがDB内に見つかりませんでした…💦: message_id={message_id}"
            raise TalkDataNotFoundError(error_message)
        return MessageLog(str(row[0]), int(row[1]), str(row[2]))
