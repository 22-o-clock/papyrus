"""冷笑ポイントのテーブル定義と、実行環境に対応したスキーマ接続。"""

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import cast

from sqlalchemy import BigInteger, Boolean, Date, Index, Numeric, SmallInteger, Table, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from cogs.talkdata.database import (
    TALKDATA_SCHEMA,
    DiscordChannel,
    DiscordMember,
    DiscordMessage,
    get_talkdata_schema,
)
from core.db import Base
from core.runtime_environment import BotEnvironment

from .constants import DEFAULT_HUMAN_WEIGHT, DEFAULT_PAPYRUS_WEIGHT, REACTION_SOURCE


class CynicismReaction(Base):
    """冷笑ポイントの根拠となる🥶の付与1件。"""

    __tablename__ = "cynicism_reaction"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    reactor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    emoji_name: Mapped[str] = mapped_column(Text, primary_key=True)
    is_burst: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=False)
    # 同じ相手へリアクションと返信の両方で🥶を向けた場合に、片方を取り消しても他方が残るよう主キーへ含める。
    source: Mapped[str] = mapped_column(Text, primary_key=True, default=REACTION_SOURCE)
    # 返信由来の場合の返信メッセージ自身のID。リアクション由来ではNULL。
    evidence_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_author_id: Mapped[int] = mapped_column(BigInteger)
    message_author_is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    reactor_is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    # 集計期間の判定基準。平均の分母と時間軸を揃えるため、🥶を向けられた発言の投稿時刻を使う。
    message_posted_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))
    recorded_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("ix_cynicism_reaction_posted_at", "message_posted_at"),
        {"schema": TALKDATA_SCHEMA},
    )


class CynicismConfiguration(Base):
    """冷笑ポイントの重みと一時停止状態を保持する単一行の設定。"""

    __tablename__ = "cynicism_configuration"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    papyrus_weight: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=DEFAULT_PAPYRUS_WEIGHT)
    human_weight: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=DEFAULT_HUMAN_WEIGHT)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))


class CynicismReportDelivery(Base):
    """期間種別ごとの、ランキング発表メッセージの配送記録。"""

    __tablename__ = "cynicism_report_deliveries"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    period_type: Mapped[str] = mapped_column(Text, primary_key=True)
    period_start: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    target_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger)
    # 内容が変わっていない再集計でDiscordのメッセージ編集を発行しないための指紋。
    content_digest: Mapped[str] = mapped_column(Text)
    first_posted_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))
    last_updated_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))


class CynicismDatabase:
    """実行環境に対応するTalkDataスキーマへ接続し、冷笑ポイント用テーブルを準備する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        environment: BotEnvironment,
    ) -> None:
        self._session_factory = session_factory
        self._database_schema = get_talkdata_schema(environment)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """実行環境に対応するスキーマへ読み替えたSessionを返す。"""
        async with self._session_factory() as session:
            await session.connection(execution_options={"schema_translate_map": {TALKDATA_SCHEMA: self._database_schema}})
            yield session

    async def initialize(self) -> None:
        """冷笑ポイント用テーブルと、集計に必要なインデックスを冪等に用意する。"""
        if not self._database_schema.isidentifier():
            message = "Schema name must be a valid identifier"
            raise ValueError(message)
        async with self.session() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        # talkdata cogを読み込まない構成でも分母クエリが動くよう、参照するテーブルも用意する。
                        cast("Table", DiscordMember.__table__),
                        cast("Table", DiscordChannel.__table__),
                        cast("Table", DiscordMessage.__table__),
                        cast("Table", CynicismReaction.__table__),
                        cast("Table", CynicismConfiguration.__table__),
                        cast("Table", CynicismReportDelivery.__table__),
                    ],
                )
            )
            # create_allは既存テーブルへインデックスを追加しないため、分母クエリ用の部分インデックスは個別に作る。
            await session.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS ix_message_original_post_time ON "{self._database_schema}".message '
                    "(post_time, member_id) WHERE edit_count = 0"
                )
            )
            await session.commit()
