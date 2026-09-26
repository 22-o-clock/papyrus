"""秘密投票のテーブル定義と、実行環境に対応したスキーマ接続。"""

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, Table, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from cogs.talkdata.database import TALKDATA_SCHEMA, get_talkdata_schema
from core.db import Base
from core.runtime_environment import BotEnvironment


class Poll(Base):
    """投票の設定と、締め切り時点の集計結果。投票者に関する情報は持たない。"""

    __tablename__ = "poll"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    # SQLiteでは INTEGER PRIMARY KEY だけが自動採番されるため、テスト用に型を切り替える。
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    # 告知メッセージの投稿前はNULL。
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 直近の締め切りで投稿した結果メッセージ。再開後も前回の結果として参照する。
    result_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON)
    allow_multiple: Mapped[bool] = mapped_column(Boolean)
    # Falseの変更不可モードでは、選択を票の行に残さず live_counts へ加算する。
    allow_change: Mapped[bool] = mapped_column(Boolean)
    target_role_id: Mapped[int] = mapped_column(BigInteger)
    closes_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True))
    closed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    finalized_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # 変更不可モードで投票ごとに加算する、選択肢ごとの票数。変更可モードではNULL。
    live_counts: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    result_counts: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    result_voter_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PollBallot(Base):
    """投票者1人の票。

    投票者は秘密鍵によるHMACでだけ表し、投票時刻は保持しない。結果の確定時に全行を削除する。
    変更不可モードでは選択を持たない投票済みの印として使い、選択は投票の行の票数へ加算する。
    同じトランザクションで書き込んだ行どうしはトランザクション番号で対応付けられるため、
    選択を別の行として残してはならない。
    """

    __tablename__ = "poll_ballot"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    poll_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{TALKDATA_SCHEMA}.poll.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    voter_key: Mapped[str] = mapped_column(Text, primary_key=True)
    choices: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)


class PollElectorate(Base):
    """投票の作成時点で対象ロールを持っていた、投票できるメンバー。

    サーバー内で公開されている情報であり、誰でも確認できるよう確定後も残す。
    """

    __tablename__ = "poll_electorate"
    __table_args__ = ({"schema": TALKDATA_SCHEMA},)

    poll_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{TALKDATA_SCHEMA}.poll.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)


class PollDatabase:
    """実行環境に対応するTalkDataスキーマへ接続し、秘密投票用テーブルを準備する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], environment: BotEnvironment) -> None:
        """セッションファクトリと実行環境から、利用するDBスキーマを決定する。"""
        self._session_factory = session_factory
        self._database_schema = get_talkdata_schema(environment)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """実行環境に対応するスキーマへ読み替えたSessionを返し、終了時にコミットする。

        スキーマの読み替えはコネクション単位の設定なので、ブロック全体を1つのトランザクションに閉じ込める。
        """
        async with self._session_factory() as session, session.begin():
            await session.connection(execution_options={"schema_translate_map": {TALKDATA_SCHEMA: self._database_schema}})
            yield session

    async def initialize(self) -> None:
        """秘密投票用テーブルを冪等に用意する。"""
        async with self.session() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        cast("Table", Poll.__table__),
                        cast("Table", PollBallot.__table__),
                        cast("Table", PollElectorate.__table__),
                    ],
                )
            )
