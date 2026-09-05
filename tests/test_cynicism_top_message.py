"""最多ポイントの発言を選ぶクエリと、ランキングへの統合を検証する。"""

import datetime
import sqlite3
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from sqlalchemy.dialects import sqlite
from sqlalchemy.sql import Select

from cogs.cynicism.models import ChannelScope, MemberReactionCounts, MessageReactionCounts
from cogs.cynicism.periods import CynicismPeriodType, period_from_start_date
from cogs.cynicism.repositories.reaction import CynicismReactionRepository
from cogs.cynicism.use_cases.reporting import CynicismReportUseCases

PAPYRUS_USER_ID = 100


def ensure(condition: object) -> None:
    """条件が成立しなければ検証を失敗させる。"""
    if not condition:
        raise AssertionError


class QueryDatabase:
    """外部DBへ接続せず、最多リアクション発言のSQLを実行する。"""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            "ATTACH DATABASE ':memory:' AS talkdata;"
            "CREATE TABLE talkdata.message (id INTEGER, channel_id INTEGER, member_id INTEGER, "
            "post_time TEXT, edit_count INTEGER);"
            "CREATE TABLE talkdata.cynicism_reaction (message_id INTEGER, reactor_id INTEGER, source TEXT);"
            "CREATE TABLE talkdata.cynicism_excluded_channels (channel_id INTEGER PRIMARY KEY);"
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator["QueryDatabase"]:
        """SQLを実行するセッションの代役を返す。"""
        yield self

    async def execute(self, statement: Select[Any]) -> SimpleNamespace:
        """生成されたSQLを実行し、結果を返す。"""
        sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        rows = self.connection.execute(sql).fetchall()
        return SimpleNamespace(all=lambda: rows)

    def add_message(self, message_id: int, *, member_id: int = 1, channel_id: int = 10, post_time: str) -> None:
        """対象の投稿と編集履歴を追加する。"""
        self.connection.executemany(
            "INSERT INTO talkdata.message VALUES (?, ?, ?, ?, ?)",
            [(message_id, channel_id, member_id, post_time, edit) for edit in (0, 1)],
        )

    def add_reactions(self, message_id: int, reactor_ids: Sequence[int], *, source: str = "reaction") -> None:
        """リアクションの記録を追加する。重複は通常・スーパーリアクションを想定する。"""
        self.connection.executemany(
            "INSERT INTO talkdata.cynicism_reaction VALUES (?, ?, ?)",
            [(message_id, reactor_id, source) for reactor_id in reactor_ids],
        )


class TopMessageQueryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = QueryDatabase()
        self.addCleanup(self.database.connection.close)
        self.repository = CynicismReactionRepository(cast("Any", self.database))
        self.period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

    async def top(self, *, member_ids: Sequence[int] = (1, 2), debug: bool = False) -> list[MessageReactionCounts]:
        """集計範囲を指定して最多リアクションの発言を問い合わせる。"""
        return await self.repository.most_reacted_messages(
            self.period,
            member_ids=member_ids,
            scope=ChannelScope(frozenset({10}) if debug else None, frozenset({99})),
            papyrus_user_id=100,
        )

    async def test_excludes_legacy_bot_self_replies_and_duplicate_reactions(self) -> None:
        for message_id in (1, 2):
            self.database.add_message(message_id, post_time="2026-07-25 00:00:00.000000")
        self.database.add_reactions(1, [1, 100, 3, 3])
        self.database.add_reactions(1, [4, 5, 6], source="reply")
        self.database.add_reactions(2, [3, 4])
        ensure(await self.top() == [MessageReactionCounts(2, 10, 1, 2)])
        self.database.connection.execute("DELETE FROM talkdata.cynicism_reaction WHERE message_id = 2")
        ensure(await self.top() == [MessageReactionCounts(1, 10, 1, 1)])

    async def test_persisted_exclusion_applies_in_debug_and_is_reversible(self) -> None:
        self.database.add_message(1, post_time="2026-07-25 00:00:00.000000")
        self.database.add_reactions(1, [3, 4])
        expected = [MessageReactionCounts(1, 10, 1, 2)]
        ensure(await self.top(debug=True) == expected)
        self.database.connection.execute("INSERT INTO talkdata.cynicism_excluded_channels VALUES (10)")
        ensure(await self.top(debug=True) == [])
        ensure(await self.top() == [])
        self.database.connection.execute("DELETE FROM talkdata.cynicism_excluded_channels WHERE channel_id = 10")
        ensure(await self.top(debug=True) == expected)

    async def test_period_scope_and_eligible_authors_are_applied(self) -> None:
        rows = (
            (1, 1, 10, "2026-07-24 22:00:00.000000"),
            (2, 1, 10, "2026-07-24 21:59:59.999999"),
            (3, 1, 10, "2026-07-31 22:00:00.000000"),
            (4, 1, 99, "2026-07-25 00:00:00.000000"),
            (5, 100, 10, "2026-07-25 00:00:00.000000"),
            (0, 1, 10, "2026-07-25 00:00:00.000000"),
        )
        for message_id, member_id, channel_id, post_time in rows:
            self.database.add_message(message_id, member_id=member_id, channel_id=channel_id, post_time=post_time)
            self.database.add_reactions(message_id, [3] if message_id == 1 else [3, 4, 5])
        for debug in (False, True):
            ensure(await self.top(debug=debug) == [MessageReactionCounts(1, 10, 1, 1)])

    async def test_returns_all_tied_top_messages_ordered_by_post_time_then_id(self) -> None:
        for message_id, post_time in ((3, "2026-07-26"), (2, "2026-07-25"), (1, "2026-07-25"), (4, "2026-07-24 23:00")):
            self.database.add_message(message_id, member_id=2 if message_id == 2 else 1, post_time=post_time)  # noqa: PLR2004
            self.database.add_reactions(message_id, [5] if message_id == 4 else [5, 6])  # noqa: PLR2004
        ensure(
            await self.top()
            == [MessageReactionCounts(1, 10, 1, 2), MessageReactionCounts(2, 10, 2, 2), MessageReactionCounts(3, 10, 1, 2)]
        )

    async def test_empty_period_or_no_eligible_authors_has_no_top_message(self) -> None:
        ensure(await self.top() == [])
        self.database.add_message(1, post_time="2026-07-25")
        self.database.add_reactions(1, [3])
        ensure(await self.top(member_ids=[]) == [])


class TopMessageRankingTest(unittest.IsolatedAsyncioTestCase):
    async def test_unqualified_member_can_have_top_message_but_bot_cannot(self) -> None:
        use_cases = object.__new__(CynicismReportUseCases)
        use_cases._runtime_environment = cast(  # noqa: SLF001
            "Any", SimpleNamespace(is_debug=False, chatbot_test_channel_ids=frozenset({99}))
        )
        use_cases._server_id = 500  # noqa: SLF001
        use_cases._bot = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(
                user=SimpleNamespace(id=100),
                get_guild=lambda _: None,
                get_user=lambda member_id: SimpleNamespace(display_name=str(member_id), bot=member_id == PAPYRUS_USER_ID),
            ),
        )
        reactions = SimpleNamespace(
            aggregate_counts=AsyncMock(return_value=[MemberReactionCounts(i, 3, 1) for i in (1, 2, 100)]),
            aggregate_message_counts=AsyncMock(return_value={1: 10, 2: 1, 100: 20}),
            get_display_names=AsyncMock(return_value={}),
            most_reacted_messages=AsyncMock(
                return_value=[MessageReactionCounts(20, 10, 2, 3), MessageReactionCounts(21, 10, 1, 3)]
            ),
        )
        use_cases._reactions = cast("Any", reactions)  # noqa: SLF001
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
        ranking = await use_cases.build_ranking_for(period)
        ensure(ranking.rate_champion is not None and ranking.rate_champion.member_id == 1)
        ensure(ranking.top_messages and ranking.top_messages[0].member_id == 2)  # noqa: PLR2004
        ensure(ranking.top_messages and ranking.top_messages[0].points == 3)  # noqa: PLR2004
        ensure([top.message_id for top in ranking.top_messages] == [20, 21])
        ensure(reactions.most_reacted_messages.await_args.kwargs["member_ids"] == [1, 2])
        ensure(ranking.top_messages and ranking.top_messages[0].jump_url == "https://discord.com/channels/500/10/20")
