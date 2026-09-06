"""永続的な除外設定と、権限を要求しない管理コマンドを検証する。"""

import datetime
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
from sqlalchemy import Table, create_engine, select

from cogs.cynicism.database import CynicismExcludedChannel
from cogs.cynicism.models import ChannelScope
from cogs.cynicism.periods import CynicismPeriodType, period_from_start_date
from cogs.cynicism.repositories.exclusions import CynicismExclusionRepository, ExcludedChannel
from cogs.cynicism.repositories.reaction import _scope_conditions
from cogs.cynicism.services.ranking import build_ranking
from cogs.cynicism.use_cases.exclusions import CynicismExclusionUseCases
from cogs.cynicism.use_cases.reporting import CynicismReportUseCases
from cogs.talkdata.database import DiscordMessage
from core.exception import ArgumentError

EMBED_LIMIT = 4096
CHOICE_LIMIT = 25
CHOICE_NAME_LIMIT = 100


def ensure(condition: object) -> None:
    """条件が成立しなければ検証を失敗させる。"""
    if not condition:
        raise AssertionError


class SqliteDatabase:
    """除外設定のSQLを実行し、セッション終了時にコミットするテスト用DB。"""

    def __init__(self) -> None:
        """独立したメモリDBへ必要なテーブルを作る。"""
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS talkdata")
            cast("Table", CynicismExcludedChannel.__table__).create(connection)
            connection.exec_driver_sql("CREATE TABLE talkdata.message (channel_id INTEGER)")
            connection.exec_driver_sql("INSERT INTO talkdata.message VALUES (10), (11), (99)")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SimpleNamespace]:
        """SQLAlchemyの同期接続をリポジトリの非同期インターフェースへ適合させる。"""
        with self.engine.begin() as connection:
            yield SimpleNamespace(
                execute=AsyncMock(side_effect=connection.execute), scalar=AsyncMock(side_effect=connection.scalar)
            )


class ExclusionRepositoryTest(unittest.IsolatedAsyncioTestCase):
    """セッションをまたぐ保存、サーバー分離、再集計時の除外を確認する。"""

    def setUp(self) -> None:
        self.database = SqliteDatabase()
        self.addCleanup(self.database.engine.dispose)
        self.repository = CynicismExclusionRepository(cast("Any", self.database))

    async def test_persistence_upsert_and_guild_scoped_removal(self) -> None:
        await self.repository.exclude(1, 10, "before")
        await self.repository.exclude(1, 10, "renamed")
        reopened = CynicismExclusionRepository(cast("Any", self.database))
        ensure(await reopened.list_excluded(1) == [ExcludedChannel(10, "renamed")])
        ensure(await reopened.list_excluded(2) == [])
        ensure(not await reopened.include(2, 10))
        ensure(await reopened.include(1, 10))
        ensure(not await reopened.include(1, 10))
        ensure(await reopened.list_excluded(1) == [])

    async def test_exclusion_overrides_debug_inclusion_and_leaves_other_channels(self) -> None:
        async def channels(scope: ChannelScope) -> list[int]:
            """本番クエリと共通の絞り込み条件を実行する。"""
            with self.database.engine.connect() as connection:
                return list(
                    connection.scalars(
                        select(DiscordMessage.channel_id).where(*_scope_conditions(DiscordMessage.channel_id, scope))
                    )
                )

        debug = ChannelScope(frozenset({10, 11}), frozenset())
        production = ChannelScope(None, frozenset({99}))
        await self.repository.exclude(1, 10, "parent")
        ensure(await channels(debug) == [11])
        ensure(await channels(production) == [11])
        await self.repository.include(1, 10)
        ensure(await channels(debug) == [10, 11])


class ExclusionCommandsTest(unittest.IsolatedAsyncioTestCase):
    """一般メンバーの操作、削除済みID、一覧の全件表示を確認する。"""

    def setUp(self) -> None:
        self.repository = SimpleNamespace(
            exclude=AsyncMock(), include=AsyncMock(return_value=True), list_excluded=AsyncMock(return_value=[])
        )
        self.use_cases = CynicismExclusionUseCases(cast("Any", self.repository))
        target = Mock(spec=discord.Thread)
        target.id, target.name, target.guild = 10, "thread", SimpleNamespace(id=1)
        self.interaction = SimpleNamespace(
            guild_id=1,
            channel_id=10,
            channel=target,
            user=SimpleNamespace(roles=[]),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_non_admin_can_exclude_current_thread_and_remove_archived_id(self) -> None:
        await self.use_cases.exclude(cast("Any", self.interaction), None)
        self.repository.exclude.assert_awaited_once_with(1, 10, "thread")
        self.interaction.response.defer.assert_awaited_once_with(thinking=True)
        ensure(not self.interaction.followup.send.await_args.kwargs.get("ephemeral"))
        await self.use_cases.include(cast("Any", self.interaction), "20")
        self.repository.include.assert_awaited_once_with(1, 20)
        self.interaction.response.defer.assert_awaited_with(thinking=True)
        ensure(not self.interaction.followup.send.await_args.kwargs.get("ephemeral"))

    async def test_rejects_dm_foreign_guild_and_invalid_id_before_writes(self) -> None:
        self.interaction.guild_id = None
        with self.assertRaises(ArgumentError):  # noqa: PT027 - unittestで実行する。
            await self.use_cases.exclude(cast("Any", self.interaction), None)
        self.interaction.guild_id = 2
        with self.assertRaises(ArgumentError):  # noqa: PT027 - unittestで実行する。
            await self.use_cases.exclude(cast("Any", self.interaction), None)
        for value in ("bad", "-1", "0", str(2**63)):
            with self.assertRaises(ArgumentError):  # noqa: PT027 - unittestで実行する。
                await self.use_cases.include(cast("Any", self.interaction), value)
        self.repository.exclude.assert_not_awaited()
        self.repository.include.assert_not_awaited()

    async def test_lists_all_exclusions_across_pages_and_bounds_autocomplete(self) -> None:
        targets = [ExcludedChannel(1000 + index, "*" * 100) for index in range(60)]
        self.repository.list_excluded.return_value = targets
        await self.use_cases.list_excluded(cast("Any", self.interaction))
        ensure(all(call.kwargs["ephemeral"] for call in self.interaction.followup.send.await_args_list))
        embeds = [call.kwargs["embed"] for call in self.interaction.followup.send.await_args_list]
        ensure(len(embeds) > 1)
        ensure(all(len(embed.description) <= EMBED_LIMIT for embed in embeds))
        text = "\n".join(embed.description for embed in embeds)
        for target in targets:
            ensure(text.count(f"<#{target.channel_id}>") == 1)
        choices = await self.use_cases.autocomplete(cast("Any", self.interaction), "")
        ensure(len(choices) == CHOICE_LIMIT)
        ensure(all(len(choice.name) <= CHOICE_NAME_LIMIT for choice in choices))
        choices = await self.use_cases.autocomplete(cast("Any", self.interaction), "1059")
        ensure([choice.value for choice in choices] == ["1059"])


class EmptyReportAfterExclusionTest(unittest.IsolatedAsyncioTestCase):
    """除外で全件が消えた場合も、既存の発表から古い順位と添付を取り除く。"""

    async def test_updates_existing_report_without_publishing_new_empty_periods(self) -> None:
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
        for posted in (False, True):
            use_cases = cast("Any", object.__new__(CynicismReportUseCases))
            use_cases.build_ranking_for = AsyncMock(return_value=build_ranking(period, [], {}, {}))
            use_cases._target_id = 1  # noqa: SLF001
            reports = SimpleNamespace(
                get_delivery=AsyncMock(return_value=SimpleNamespace(is_posted=posted)),
                save_empty=AsyncMock(),
                save_posted=AsyncMock(),
            )
            delivery = SimpleNamespace(
                upsert=AsyncMock(
                    return_value=SimpleNamespace(
                        message=SimpleNamespace(id=10),
                        changed=True,
                        updated_at=datetime.datetime.now(datetime.UTC),
                    )
                )
            )
            use_cases._reports = reports  # noqa: SLF001
            use_cases._delivery = delivery  # noqa: SLF001
            result = await use_cases._post_or_update(period, record_empty=True)  # noqa: SLF001
            if posted:
                ensure(result.id == 10)  # noqa: PLR2004 - テスト用発表ID。
                ensure(delivery.upsert.await_args.kwargs["files"] == [])
                embed = delivery.upsert.await_args.args[1]
                ensure(not any("参考: 合計" in field.name or "最多ポイント" in field.name for field in embed.fields))
                reports.save_posted.assert_awaited_once()
                reports.save_empty.assert_not_awaited()
            else:
                ensure(result is None)
                delivery.upsert.assert_not_awaited()
                reports.save_empty.assert_awaited_once()
