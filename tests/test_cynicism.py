import asyncio
import datetime
import io
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import discord

from cogs.cynicism.constants import CUSTOM_CYNICISM_EMOJI_NAME, CYNICISM_EMOJI, JST, REACTION_SOURCE
from cogs.cynicism.models import (
    ChannelScope,
    CynicismMessageRecord,
    CynicismSettings,
    MemberReactionCounts,
    RankedMemberIdentity,
    ReactorContribution,
    TopCynicismMessage,
)
from cogs.cynicism.periods import (
    CynicismPeriod,
    CynicismPeriodType,
    format_period,
    latest_completed_period,
    period_containing,
    period_from_start_date,
    qualification_threshold,
)
from cogs.cynicism.repositories.configuration import CynicismConfigurationRepository
from cogs.cynicism.repositories.reaction import CynicismReactionEvent, CynicismReactionRepository
from cogs.cynicism.services.message_delivery import (
    CynicismReportMessageDelivery,
    ReportMessageOwnershipError,
)
from cogs.cynicism.services.message_list import (
    EMBED_DESCRIPTION_LIMIT,
    MESSAGE_PREVIEW_LENGTH,
    build_message_embeds,
)
from cogs.cynicism.services.ranking import build_ranking, cynicism_rate
from cogs.cynicism.services.reaction_filter import is_cynicism_emoji
from cogs.cynicism.services.report_builder import (
    build_empty_notice,
    build_ranking_embed,
    build_report_files,
    build_top_messages_files,
    ranking_digest,
    report_marker,
)
from cogs.cynicism.services.schedule import publish_time, publishable_periods, refreshable_periods
from cogs.cynicism.services.scope import channel_scope
from cogs.cynicism.use_cases.reporting import CynicismReportUseCases
from cogs.cynicism.use_cases.tracking import CynicismTrackingUseCases
from core.exception import ArgumentError

if TYPE_CHECKING:
    from cogs.cynicism.models import CynicismRanking

PAPYRUS_USER_ID = 100
HUMAN_USER_ID = 200
AUTHOR_USER_ID = 300
CHANNEL_ID = 400
GUILD_ID = 500
TEST_CHANNEL_ID = 900

WEEKLY_THRESHOLD = 10
MONTHLY_THRESHOLD = 30
YEARLY_THRESHOLD = 100
FEBRUARY_LEAP_LAST_DAY = 29
EXPECTED_PUBLISHABLE_PERIOD_COUNT = 3
HEAVY_POSTER_ID = 2
LIGHT_POSTER_ID = 1
TARGET_MESSAGE_ID = 12345


def ensure(condition: object, message: str = "") -> None:
    """条件を満たさない場合にテストを失敗させます。"""
    if not condition:
        raise AssertionError(message)


def make_settings(*, is_paused: bool = False) -> CynicismSettings:
    """テスト用の運用設定を組み立てます。"""
    return CynicismSettings(
        is_paused=is_paused,
        paused_at=None,
    )


class PeriodTest(unittest.TestCase):
    def test_week_starts_on_friday_at_22(self) -> None:
        # 2026-07-26は日曜。直前の切り替えは2026-07-24 (金) 22:00。
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(period.start_at == datetime.datetime(2026, 7, 24, 22, 0, tzinfo=JST))
        ensure(period.end_at == datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST))

    def test_friday_before_the_switch_belongs_to_the_previous_week(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.datetime(2026, 7, 24, 21, 59, tzinfo=JST))

        ensure(period.start_at == datetime.datetime(2026, 7, 17, 22, 0, tzinfo=JST))
        ensure(period.end_at == datetime.datetime(2026, 7, 24, 22, 0, tzinfo=JST))

    def test_the_switch_moment_starts_the_new_week(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.datetime(2026, 7, 24, 22, 0, tzinfo=JST))

        ensure(period.start_at == datetime.datetime(2026, 7, 24, 22, 0, tzinfo=JST))

    def test_week_spans_the_new_year(self) -> None:
        # 2026-01-01は木曜なので、直前の切り替えは2025-12-26 (金) 22:00。
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 1, 1))

        ensure(period.start_at == datetime.datetime(2025, 12, 26, 22, 0, tzinfo=JST))
        ensure(period.end_at == datetime.datetime(2026, 1, 2, 22, 0, tzinfo=JST))

    def test_a_start_date_from_the_display_selects_that_week(self) -> None:
        """表示された開始日を指定すると、その週が対象になる。"""
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 24))

        ensure(period.start_at == datetime.datetime(2026, 7, 24, 22, 0, tzinfo=JST))

    def test_month_covers_the_last_day(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(period.start_date == datetime.date(2026, 7, 1))
        ensure(period.end_date == datetime.date(2026, 7, 31))

    def test_february_of_a_leap_year_ends_on_the_29th(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2024, 2, 10))

        ensure(period.end_date.day == FEBRUARY_LEAP_LAST_DAY)

    def test_year_covers_the_whole_calendar_year(self) -> None:
        period = period_from_start_date(CynicismPeriodType.YEARLY, datetime.date(2026, 7, 26))

        ensure(period.start_date == datetime.date(2026, 1, 1))
        ensure(period.end_date == datetime.date(2026, 12, 31))

    def test_monthly_boundaries_are_jst_midnight_and_exclusive_at_the_end(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(period.start_at == datetime.datetime(2026, 7, 1, tzinfo=JST))
        ensure(period.end_at == datetime.datetime(2026, 8, 1, tzinfo=JST))

    def test_qualification_thresholds_match_the_agreed_values(self) -> None:
        target = datetime.date(2026, 7, 26)

        ensure(qualification_threshold(period_from_start_date(CynicismPeriodType.WEEKLY, target)) == WEEKLY_THRESHOLD)
        ensure(qualification_threshold(period_from_start_date(CynicismPeriodType.MONTHLY, target)) == MONTHLY_THRESHOLD)
        ensure(qualification_threshold(period_from_start_date(CynicismPeriodType.YEARLY, target)) == YEARLY_THRESHOLD)

    def test_weekly_format_shows_the_switch_times(self) -> None:
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(format_period(period) == "2026-07-24 22:00 〜 2026-07-31 22:00 (JST)")

    def test_monthly_format_shows_the_inclusive_date_range(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(format_period(period) == "2026-07-01 〜 2026-07-31 (JST)")


class ScheduleTest(unittest.TestCase):
    def test_weekly_is_published_at_the_switch_moment(self) -> None:
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(publish_time(period) == period.end_at)
        ensure(publish_time(period) == datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST))

    def test_monthly_is_published_on_the_next_day_at_21(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(publish_time(period) == datetime.datetime(2026, 8, 1, 21, 0, tzinfo=JST))

    def test_week_is_not_published_before_the_switch(self) -> None:
        just_before = datetime.datetime(2026, 7, 31, 21, 59, tzinfo=JST)

        periods = publishable_periods(just_before, datetime.date(2026, 7, 24))

        ensure(all(period.start_date != datetime.date(2026, 7, 24) for period in periods))

    def test_week_is_published_at_the_switch(self) -> None:
        at_the_switch = datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST)

        periods = publishable_periods(at_the_switch, datetime.date(2026, 7, 24))

        ensure(any(period.start_date == datetime.date(2026, 7, 24) for period in periods))

    def test_periods_before_the_first_record_are_skipped(self) -> None:
        now = datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST)

        periods = publishable_periods(now, datetime.date(2026, 7, 25))

        ensure(len(periods) == 1)
        ensure(periods[0].period_type is CynicismPeriodType.WEEKLY)

    def test_no_record_still_limits_backfill(self) -> None:
        now = datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST)

        periods = publishable_periods(now, None)

        weekly = [period for period in periods if period.period_type is CynicismPeriodType.WEEKLY]
        ensure(len(weekly) == 8)  # noqa: PLR2004 - MAXIMUM_BACKFILL_PERIODSの週次上限。

    def test_all_three_period_types_can_publish_together(self) -> None:
        # 2024-01-05 (金) 22:00で週が切り替わり、月次・年次も2024-01-01 21:00を過ぎている。
        now = datetime.datetime(2024, 1, 5, 22, 30, tzinfo=JST)

        periods = publishable_periods(now, datetime.date(2023, 1, 1))
        published_types = {period.period_type for period in periods}

        ensure(len(published_types) == EXPECTED_PUBLISHABLE_PERIOD_COUNT)

    def test_publishable_periods_are_ordered_from_oldest(self) -> None:
        now = datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST)

        periods = publishable_periods(now, None)
        weekly = [period.start_date for period in periods if period.period_type is CynicismPeriodType.WEEKLY]

        ensure(weekly == sorted(weekly))

    def test_refreshable_periods_cover_recent_completed_periods(self) -> None:
        now = datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST)

        periods = refreshable_periods(now)
        weekly = [period for period in periods if period.period_type is CynicismPeriodType.WEEKLY]

        ensure(len(weekly) == 4)  # noqa: PLR2004 - REFRESH_PERIOD_COUNTSの週次件数。
        ensure(weekly[0] == latest_completed_period(CynicismPeriodType.WEEKLY, now))


class ReactionFilterTest(unittest.TestCase):
    def test_unicode_cynicism_emoji_is_accepted(self) -> None:
        emoji = discord.PartialEmoji(name=CYNICISM_EMOJI)

        ensure(is_cynicism_emoji(emoji))

    def test_other_unicode_emoji_is_rejected(self) -> None:
        ensure(not is_cynicism_emoji(discord.PartialEmoji(name="😀")))

    def test_custom_emoji_with_the_same_name_is_rejected(self) -> None:
        emoji = discord.PartialEmoji(name=CYNICISM_EMOJI, id=1234)

        ensure(not is_cynicism_emoji(emoji))

    def test_target_custom_emoji_is_accepted(self) -> None:
        emoji = discord.PartialEmoji(name=CUSTOM_CYNICISM_EMOJI_NAME, id=1234)

        ensure(is_cynicism_emoji(emoji))

    def test_other_custom_emoji_is_rejected(self) -> None:
        emoji = discord.PartialEmoji(name="other_emoji", id=1234)

        ensure(not is_cynicism_emoji(emoji))

    def test_bare_name_without_id_is_rejected(self) -> None:
        # `:name:`表記の絵文字IDが取れない場合、カスタム絵文字とは判定しない。
        emoji = discord.PartialEmoji(name=CUSTOM_CYNICISM_EMOJI_NAME)

        ensure(not is_cynicism_emoji(emoji))


class ChannelScopeTest(unittest.TestCase):
    def test_production_excludes_the_chatbot_test_channel(self) -> None:
        runtime = cast(
            "Any",
            SimpleNamespace(is_debug=False, chatbot_test_channel_ids=frozenset({TEST_CHANNEL_ID})),
        )

        scope = channel_scope(runtime)

        ensure(scope.contains(CHANNEL_ID))
        ensure(not scope.contains(TEST_CHANNEL_ID))

    def test_debug_targets_only_the_chatbot_test_channel(self) -> None:
        runtime = cast(
            "Any",
            SimpleNamespace(is_debug=True, chatbot_test_channel_ids=frozenset({TEST_CHANNEL_ID})),
        )

        scope = channel_scope(runtime)

        ensure(scope.contains(TEST_CHANNEL_ID))
        ensure(not scope.contains(CHANNEL_ID))


class RankingTest(unittest.TestCase):
    def test_rate_is_zero_when_the_member_has_no_messages(self) -> None:
        ensure(cynicism_rate(5, 0) == 0.0)

    def test_rate_champion_can_differ_from_the_total_champion(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, human_count=4, cynical_message_count=2),
            MemberReactionCounts(member_id=2, human_count=10, cynical_message_count=5),
        ]
        identities = {
            1: RankedMemberIdentity(1, "少数精鋭", is_bot=False),
            2: RankedMemberIdentity(2, "数打ち", is_bot=False),
        }
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
        ranking = build_ranking(period, counts, {1: 10, 2: 100}, identities)
        ensure(ranking.rate_champion is not None and ranking.rate_champion.member_id == LIGHT_POSTER_ID)
        ensure(ranking.total_champion is not None and ranking.total_champion.member_id == HEAVY_POSTER_ID)
        embed = build_ranking_embed(ranking, updated_at=period.end_at)
        ensure(embed.fields[0].name == "👑 冷笑王 (冷笑率)")
        ensure(embed.fields[0].value is not None and "少数精鋭" in embed.fields[0].value)
        ensure(embed.fields[1].name is not None and embed.fields[1].name.startswith("冷笑率ランキング"))

    def test_bot_authors_are_excluded_from_the_ranking(self) -> None:
        counts = [MemberReactionCounts(member_id=1, human_count=3, cynical_message_count=1)]
        identities = {1: RankedMemberIdentity(1, "Bot", is_bot=True)}
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 100}, identities)

        ensure(ranking.is_empty)

    def test_tied_totals_share_the_same_rank(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, human_count=3, cynical_message_count=1),
            MemberReactionCounts(member_id=2, human_count=3, cynical_message_count=1),
            MemberReactionCounts(member_id=3, human_count=1, cynical_message_count=1),
        ]
        identities = {
            1: RankedMemberIdentity(1, "A", is_bot=False),
            2: RankedMemberIdentity(2, "B", is_bot=False),
            3: RankedMemberIdentity(3, "C", is_bot=False),
        }
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 20, 2: 20, 3: 20}, identities)

        ensure([entry.rank for entry in ranking.total_entries] == [1, 1, 3])

    def test_members_below_the_threshold_stay_in_the_total_ranking_only(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, human_count=3, cynical_message_count=1),
            MemberReactionCounts(member_id=2, human_count=3, cynical_message_count=1),
        ]
        identities = {
            1: RankedMemberIdentity(1, "常連", is_bot=False),
            2: RankedMemberIdentity(2, "一言だけ", is_bot=False),
        }
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 40, 2: 1}, identities)

        ensure({entry.member_id for entry in ranking.total_entries} == {1, 2})
        ensure([entry.member_id for entry in ranking.rate_entries] == [1])
        ensure(ranking.qualified_member_count == 1)

    def test_summary_is_derived_from_ranking_entries(self) -> None:
        counts = [MemberReactionCounts(member_id=1, human_count=9, cynical_message_count=4)]
        identities = {1: RankedMemberIdentity(1, "A", is_bot=False)}
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 20}, identities)

        ensure(ranking.member_count == 1)
        ensure(ranking.total_points == Decimal("9.00"))
        empty = replace(ranking, total_entries=(), rate_entries=())
        ensure(empty.total_points == 0)
        ensure(empty.member_count == 0)


def build_sample_ranking() -> "CynicismRanking":
    """Embed・digestのテストで使う代表的なランキングを返します。"""
    counts = [
        MemberReactionCounts(member_id=1, human_count=10, cynical_message_count=3),
        MemberReactionCounts(member_id=2, human_count=2, cynical_message_count=2),
    ]
    identities = {
        1: RankedMemberIdentity(1, "冷笑家", is_bot=False),
        2: RankedMemberIdentity(2, "ときどき", is_bot=False),
    }
    period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
    return build_ranking(period, counts, {1: 40, 2: 30}, identities)


class ReportBuilderTest(unittest.TestCase):
    def test_summary_shows_points_and_qualification_counts_without_reaction_count(self) -> None:
        """サマリの文面・改行と資格人数を検証し、重複するリアクション件数の再表示を防ぐ。"""
        sample = build_sample_ranking()
        counts = [MemberReactionCounts(1, 10, 3), MemberReactionCounts(2, 2, 2)]
        identities = {
            entry.member_id: RankedMemberIdentity(entry.member_id, entry.display_name, is_bot=False)
            for entry in sample.total_entries
        }
        for message_counts, qualified_count in (({1: 10, 2: 1}, 1), ({1: 1, 2: 1}, 0), ({1: 10, 2: 10}, 2)):
            with self.subTest(qualified_count=qualified_count):
                ranking = build_ranking(sample.period, counts, message_counts, identities)
                ranking = replace(
                    ranking,
                    reactor_contributions=(ReactorContribution(3, "付与者A", 8), ReactorContribution(4, "付与者B", 4)),
                    excluded_channel_count=2,
                )
                embed = build_ranking_embed(ranking, updated_at=ranking.period.end_at)
                summary = next(field for field in embed.fields if field.name == "サマリ")

                ensure(
                    summary.value
                    == (
                        "総ポイント 12 pt (付与者A 8pt、付与者B 4pt)\n"
                        f"対象 2名 / 資格ライン到達 {qualified_count}名 / 除外対象 2件 (チャンネル・スレッド)"
                    ),
                    f"サマリの出力が想定と異なります: {summary.value!r}",
                )

    def test_long_reactor_breakdown_is_attached_without_losing_entries(self) -> None:
        ranking = replace(
            build_sample_ranking(),
            reactor_contributions=tuple(ReactorContribution(index, "付与者" * 20 + str(index), 1) for index in range(50)),
        )
        embed = build_ranking_embed(ranking, updated_at=ranking.period.end_at)
        summary = next(field for field in embed.fields if field.name == "サマリ")
        ensure(len(summary.value or "") <= 1024)  # noqa: PLR2004 - Discordのフィールド上限。
        files = build_report_files(ranking)
        ensure(len(files) == 1)
        content = files[0].fp.read().decode("utf-8")
        ensure(len(content.splitlines()) == len(ranking.reactor_contributions))
        for entry in ranking.reactor_contributions:
            ensure(f"{entry.display_name}: {entry.points}pt" in content)

    def test_reactor_breakdown_and_exclusion_count_change_digest(self) -> None:
        ranking = build_sample_ranking()
        ensure(ranking_digest(ranking) != ranking_digest(replace(ranking, excluded_channel_count=1)))
        ensure(
            ranking_digest(ranking)
            != ranking_digest(replace(ranking, reactor_contributions=(ReactorContribution(3, "付与者", 12),)))
        )

    def test_marker_identifies_the_period_type_and_start(self) -> None:
        period = period_from_start_date(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(report_marker(period) == "cynicism-report:monthly:2026-07-01")

    def test_embed_shows_rate_champion_and_reference_points(self) -> None:
        ranking = build_sample_ranking()

        embed = build_ranking_embed(ranking, updated_at=datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST))

        ensure(embed.title == "週間冷笑王")
        ensure(embed.description is not None and embed.description.startswith("2026-07-24 22:00 〜 2026-07-31 22:00 (JST)"))
        field_names = [field.name for field in embed.fields]
        ensure(any(name is not None and "参考: 合計ポイントランキング" in name for name in field_names))
        ensure(any(name is not None and "冷笑王 (冷笑率)" in name for name in field_names))
        footer_text = embed.footer.text
        if footer_text is None:
            self.fail("フッターに識別子が必要です")
        ensure(report_marker(ranking.period) in footer_text)
        ensure("重み" not in footer_text)

    def test_rate_champion_is_absent_when_nobody_qualifies(self) -> None:
        counts = [MemberReactionCounts(member_id=1, human_count=3, cynical_message_count=1)]
        identities = {1: RankedMemberIdentity(1, "一言だけ", is_bot=False)}
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
        ranking = build_ranking(period, counts, {1: 1}, identities)

        embed = build_ranking_embed(ranking, updated_at=datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST))

        rate_field = next(field for field in embed.fields if field.name is not None and "冷笑王" in field.name)
        ensure(rate_field.value is not None and "該当なし" in rate_field.value)

    def test_digest_is_stable_for_the_same_ranking(self) -> None:
        ensure(ranking_digest(build_sample_ranking()) == ranking_digest(build_sample_ranking()))

    def test_top_message_is_linked_and_changes_the_digest(self) -> None:
        ranking = build_sample_ranking()
        top = TopCynicismMessage(
            message_id=TARGET_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            member_id=AUTHOR_USER_ID,
            display_name="発言者",
            points=4,
            guild_id=GUILD_ID,
        )
        other = replace(top, message_id=TARGET_MESSAGE_ID + 1, display_name="別の発言者")
        ranking = replace(ranking, top_messages=(top, other))
        embed = build_ranking_embed(ranking, updated_at=ranking.period.end_at)
        field = next(field for field in embed.fields if field.name == "🥶 最多ポイントの発言")
        ensure(field.value is not None and "4 pt" in field.value and top.jump_url in field.value)
        ensure(field.value is not None and other.jump_url in field.value and other.display_name in field.value)
        ensure(build_top_messages_files(ranking) == [])
        for changed in (replace(other, message_id=1), replace(other, points=5), replace(other, display_name="新しい名前")):
            ensure(ranking_digest(ranking) != ranking_digest(replace(ranking, top_messages=(top, changed))))
        ensure(ranking_digest(ranking) != ranking_digest(replace(ranking, top_messages=(top,))))
        ensure(ranking_digest(ranking) != ranking_digest(replace(ranking, top_messages=())))

    def test_many_tied_messages_are_all_included_in_the_attachment(self) -> None:
        ranking = build_sample_ranking()
        top_messages = tuple(TopCynicismMessage(i, CHANNEL_ID, AUTHOR_USER_ID, f"発言者{i}", 1, GUILD_ID) for i in range(100))
        ranking = replace(ranking, top_messages=top_messages)
        embed = build_ranking_embed(ranking, updated_at=ranking.period.end_at)
        field = next(field for field in embed.fields if field.name == "🥶 最多ポイントの発言")
        ensure(field.value is not None and "添付ファイル" in field.value)
        ensure(len(embed) <= 6000)  # noqa: PLR2004 - DiscordのEmbed文字数上限。
        ensure(all(len(field.value or "") <= 1024 for field in embed.fields))  # noqa: PLR2004 - フィールド値の上限。
        files = build_top_messages_files(ranking)
        ensure(len(files) == 1)
        try:
            text = files[0].fp.read().decode("utf-8")
            ensure(len(text.splitlines()) == len(top_messages))
            ensure(all(top.jump_url in text and top.display_name in text for top in top_messages))
        finally:
            files[0].close()

    def test_top_message_section_is_absent_without_a_message(self) -> None:
        ranking = build_sample_ranking()
        embed = build_ranking_embed(ranking, updated_at=ranking.period.end_at)
        ensure(all(field.name != "🥶 最多ポイントの発言" for field in embed.fields))

    def test_empty_notice_names_the_period(self) -> None:
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure("2026-07-24 22:00 〜 2026-07-31 22:00 (JST)" in build_empty_notice(period))


def make_delivery_target(message: object | None, history: list[object] | None = None) -> Mock:
    """投稿先チャンネルの代役を返します。"""

    async def iterate_history(**_: object) -> AsyncIterator[object]:
        for entry in history or []:
            yield entry

    target = Mock(spec=discord.TextChannel)
    target.fetch_message = AsyncMock(return_value=message)
    target.send = AsyncMock(return_value=SimpleNamespace(id=999, author=SimpleNamespace(id=1)))
    target.history = iterate_history
    return target


class MessageDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_report_is_posted_and_excluded_from_long_term_memory(self) -> None:
        target = make_delivery_target(None)
        bot = cast(
            "Any",
            SimpleNamespace(user=SimpleNamespace(id=1), get_channel=Mock(return_value=target), dispatch=Mock()),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=None)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        attachment = discord.File(io.BytesIO(b"all tied messages"), filename="cynicism_top_messages.txt")
        self.addCleanup(attachment.close)
        result = await delivery.upsert(period, discord.Embed(), "digest", files=[attachment])

        ensure(result.changed)
        target.send.assert_awaited_once()
        ensure(target.send.await_args.kwargs["files"] == [attachment])
        bot.dispatch.assert_called_once()
        ensure(bot.dispatch.call_args.args[0] == "exclude_from_long_term_memory")

    async def test_unchanged_digest_does_not_edit_the_message(self) -> None:
        message = SimpleNamespace(id=55, author=SimpleNamespace(id=1), edit=AsyncMock(), embeds=[])
        target = make_delivery_target(message)
        bot = cast(
            "Any",
            SimpleNamespace(user=SimpleNamespace(id=1), get_channel=Mock(return_value=target), dispatch=Mock()),
        )
        stored = SimpleNamespace(
            message_id=55,
            content_digest="digest",
            last_processed_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        result = await delivery.upsert(period, discord.Embed(), "digest")

        ensure(not result.changed)
        message.edit.assert_not_awaited()
        bot.dispatch.assert_not_called()

    async def test_changed_digest_edits_the_message(self) -> None:
        message = SimpleNamespace(id=55, author=SimpleNamespace(id=1), edit=AsyncMock(), embeds=[])
        target = make_delivery_target(message)
        bot = cast(
            "Any",
            SimpleNamespace(user=SimpleNamespace(id=1), get_channel=Mock(return_value=target), dispatch=Mock()),
        )
        stored = SimpleNamespace(
            message_id=55,
            content_digest="old",
            last_processed_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        attachment = discord.File(io.BytesIO(b"updated tied messages"), filename="cynicism_top_messages.txt")
        self.addCleanup(attachment.close)
        result = await delivery.upsert(period, discord.Embed(), "new", files=[attachment])

        ensure(result.changed)
        message.edit.assert_awaited_once()
        ensure(message.edit.await_args.kwargs["attachments"] == [attachment])
        bot.dispatch.assert_called_once()

        await delivery.upsert(period, discord.Embed(), "fewer-ties")
        ensure(message.edit.await_args.kwargs["attachments"] == [])

    async def test_another_bots_message_is_never_overwritten(self) -> None:
        message = SimpleNamespace(id=55, author=SimpleNamespace(id=2), edit=AsyncMock(), embeds=[])
        target = make_delivery_target(message)
        bot = cast(
            "Any",
            SimpleNamespace(user=SimpleNamespace(id=1), get_channel=Mock(return_value=target), dispatch=Mock()),
        )
        stored = SimpleNamespace(
            message_id=55,
            content_digest="old",
            last_processed_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_from_start_date(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        try:
            await delivery.upsert(period, discord.Embed(), "new")
        except ReportMessageOwnershipError:
            message.edit.assert_not_awaited()
            return
        self.fail("別Botの投稿は上書きせず設定エラーにする必要があります")


def make_tracking_use_cases(
    *,
    is_paused: bool = False,
    bot_user_ids: frozenset[int] = frozenset(),
    is_debug: bool = False,
) -> tuple[CynicismTrackingUseCases, Any, Any]:
    """記録用ユースケースと、注入したBot・リポジトリの代役を返します。"""
    guild = Mock(spec=discord.Guild)
    guild.get_member = lambda user_id: SimpleNamespace(bot=True) if user_id in bot_user_ids else SimpleNamespace(bot=False)
    bot = cast(
        "Any",
        SimpleNamespace(
            user=SimpleNamespace(id=PAPYRUS_USER_ID),
            get_channel=Mock(return_value=None),
            get_user=Mock(return_value=None),
            get_guild=Mock(return_value=guild),
        ),
    )
    runtime = cast(
        "Any",
        SimpleNamespace(
            is_debug=is_debug,
            chatbot_test_channel_ids=frozenset({TEST_CHANNEL_ID}),
            should_process_chatbot_channel=lambda channel_id: channel_id != TEST_CHANNEL_ID,
        ),
    )
    reactions = cast(
        "Any",
        SimpleNamespace(
            record=AsyncMock(),
            remove_reaction=AsyncMock(),
            remove_message_reactions=AsyncMock(),
            remove_emoji_reactions=AsyncMock(),
        ),
    )
    configuration = cast("Any", SimpleNamespace(get=AsyncMock(return_value=make_settings(is_paused=is_paused))))
    return CynicismTrackingUseCases(bot, runtime, reactions, configuration), bot, reactions


def make_reaction_payload(
    *,
    emoji_name: str = CYNICISM_EMOJI,
    channel_id: int = CHANNEL_ID,
    reactor_is_bot: bool = False,
) -> discord.RawReactionActionEvent:
    """リアクションイベントの代役を返します。"""
    return cast(
        "discord.RawReactionActionEvent",
        SimpleNamespace(
            emoji=discord.PartialEmoji(name=emoji_name),
            guild_id=GUILD_ID,
            channel_id=channel_id,
            message_id=TARGET_MESSAGE_ID,
            user_id=HUMAN_USER_ID,
            message_author_id=AUTHOR_USER_ID,
            member=SimpleNamespace(bot=reactor_is_bot),
            burst=False,
        ),
    )


class TrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cynicism_reaction_is_recorded(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_awaited_once()
        event = reactions.record.await_args.args[0]
        ensure(event.message_id == TARGET_MESSAGE_ID)
        ensure(event.reactor_id == HUMAN_USER_ID)

    async def test_other_emoji_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload(emoji_name="😀"))

        reactions.record.assert_not_awaited()

    async def test_reaction_outside_the_target_channel_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload(channel_id=TEST_CHANNEL_ID))

        reactions.record.assert_not_awaited()

    async def test_reaction_on_a_bot_message_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(bot_user_ids=frozenset({AUTHOR_USER_ID}))

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_not_awaited()

    async def test_reaction_from_another_bot_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload(reactor_is_bot=True))

        reactions.record.assert_not_awaited()

    async def test_recording_does_not_fetch_the_target_message(self) -> None:
        """Discordの取得を挟まず、イベントの情報だけで記録する。"""
        use_cases, bot, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_awaited_once()
        bot.get_channel.assert_not_called()

    async def test_paused_tracking_records_nothing(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(is_paused=True)

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_not_awaited()

    async def test_paused_tracking_still_removes_cancelled_reactions(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(is_paused=True)

        await use_cases.on_reaction_remove(make_reaction_payload())

        reactions.remove_reaction.assert_awaited_once()

    async def test_papyrus_reaction_is_ignored_even_without_member_information(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()
        payload = make_reaction_payload()
        payload.user_id = PAPYRUS_USER_ID
        payload.member = None
        await use_cases.on_reaction_add(payload)
        reactions.record.assert_not_awaited()


class ReportUseCasesTest(unittest.IsolatedAsyncioTestCase):
    def build_use_cases(self, *, is_debug: bool = False, is_paused: bool = False) -> CynicismReportUseCases:
        """DBへ接続せずにレポートユースケースを組み立てます。"""
        use_cases = object.__new__(CynicismReportUseCases)
        use_cases._runtime_environment = cast("Any", SimpleNamespace(is_debug=is_debug))  # noqa: SLF001
        self.get_settings.return_value = make_settings(is_paused=is_paused)
        use_cases._configuration = cast("Any", SimpleNamespace(get=self.get_settings))  # noqa: SLF001
        use_cases._reactions = cast("Any", SimpleNamespace(earliest_recorded_date=self.earliest_recorded_date))  # noqa: SLF001
        use_cases._reports = cast("Any", SimpleNamespace(has_delivery=AsyncMock(return_value=False)))  # noqa: SLF001
        return use_cases

    def setUp(self) -> None:
        """注入した代役を、型を保ったまま検証できるよう保持します。"""
        self.get_settings = AsyncMock(return_value=make_settings())
        self.earliest_recorded_date = AsyncMock(return_value=None)

    async def test_debug_environment_never_publishes(self) -> None:
        use_cases = self.build_use_cases(is_debug=True)

        await use_cases.process_scheduled_reports()

        self.get_settings.assert_not_awaited()

    async def test_paused_aggregation_never_publishes(self) -> None:
        use_cases = self.build_use_cases(is_paused=True)

        await use_cases.process_scheduled_reports()

        self.earliest_recorded_date.assert_not_awaited()

    def test_unknown_period_type_is_rejected(self) -> None:
        use_cases = self.build_use_cases()

        try:
            use_cases._resolve_period("daily", None, default_to_completed=True)  # noqa: SLF001
        except ArgumentError:
            return
        self.fail("未知の期間種別は利用者向けエラーにする必要があります")

    def test_malformed_start_date_is_rejected(self) -> None:
        use_cases = self.build_use_cases()

        try:
            use_cases._resolve_period("weekly", "2026/07/20", default_to_completed=True)  # noqa: SLF001
        except ArgumentError:
            return
        self.fail("開始日の書式誤りは利用者向けエラーにする必要があります")

    def test_start_date_is_normalized_to_the_containing_period(self) -> None:
        use_cases = self.build_use_cases()

        period = use_cases._resolve_period("weekly", "2026-07-23", default_to_completed=True)  # noqa: SLF001

        # 2026-07-23は木曜なので、直前の切り替えである2026-07-17 (金) 22:00の週になる。
        ensure(period.start_at == datetime.datetime(2026, 7, 17, 22, 0, tzinfo=JST))


class ChannelScopeModelTest(unittest.TestCase):
    def test_included_ids_take_precedence(self) -> None:
        scope = ChannelScope(included_channel_ids=frozenset({1}), excluded_channel_ids=frozenset({1}))

        ensure(scope.contains(1))
        ensure(not scope.contains(2))


class RecordingSession:
    """実行した操作の順序を記録する、DBへ接続しないSessionの代役。"""

    def __init__(self, row: object) -> None:
        self.calls: list[str] = []
        self._row = row

    async def execute(self, _statement: object) -> SimpleNamespace:
        self.calls.append("execute")
        return SimpleNamespace(scalar_one=lambda: self._row)

    async def commit(self) -> None:
        self.calls.append("commit")


class RecordingDatabase:
    """常に同じRecordingSessionを返すCynicismDatabaseの代役。"""

    def __init__(self, session: RecordingSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[RecordingSession]:
        yield self._session


def make_configuration_row() -> SimpleNamespace:
    """設定テーブルの1行分の代役を返します。"""
    return SimpleNamespace(
        papyrus_weight=Decimal("3.00"),
        human_weight=Decimal("9.00"),
        is_paused=False,
        paused_at=None,
    )


class ConfigurationRepositoryTest(unittest.IsolatedAsyncioTestCase):
    """スキーマの読み替えはコネクション単位の設定なので、途中でコミットしてはいけない。

    コミットするとコネクションが解放され、以降の文が読み替え前のスキーマを参照して
    UndefinedTableErrorになる。
    """

    async def test_reading_settings_does_not_commit_midway(self) -> None:
        session = RecordingSession(make_configuration_row())
        repository = CynicismConfigurationRepository(cast("Any", RecordingDatabase(session)))

        settings = await repository.get()

        ensure("commit" not in session.calls, "設定の読み取りは1つのトランザクション内で完結する必要があります")
        ensure(settings == CynicismSettings(is_paused=False, paused_at=None))
        ensure(not settings.is_paused)

    async def test_updating_settings_does_not_commit_midway(self) -> None:
        session = RecordingSession(make_configuration_row())
        repository = CynicismConfigurationRepository(cast("Any", RecordingDatabase(session)))

        await repository.set_paused(paused=True, now=datetime.datetime.now(JST))

        ensure("commit" not in session.calls, "設定の更新は1つのトランザクション内で完結する必要があります")


class ReactionRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_record_supplies_legacy_storage_fields(self) -> None:
        """呼び出し側が旧返信用の情報を渡さなくても、互換列へ正しい値を保存する。"""
        session = SimpleNamespace(execute=AsyncMock())
        repository = CynicismReactionRepository(cast("Any", RecordingDatabase(cast("Any", session))))

        await repository.record(CynicismReactionEvent(TARGET_MESSAGE_ID, HUMAN_USER_ID, is_burst=True))

        statement = session.execute.await_args.args[0]
        parameters = statement.compile().params
        ensure(parameters["message_id"] == TARGET_MESSAGE_ID)
        ensure(parameters["reactor_id"] == HUMAN_USER_ID)
        ensure(parameters["is_burst"] is True)
        ensure(parameters["source"] == REACTION_SOURCE)
        ensure(parameters["evidence_message_id"] is None)


class MessageListTest(unittest.TestCase):
    def make_embeds(self, records: list[CynicismMessageRecord]) -> list[discord.Embed]:
        """同じ期間・表示名で紹介Embedを組み立てる。"""
        return build_message_embeds(
            records,
            period=build_sample_ranking().period,
            display_name="発言者",
            identities={HUMAN_USER_ID: RankedMemberIdentity(HUMAN_USER_ID, "名前付き", is_bot=False)},
            guild_id=GUILD_ID,
        )

    def test_each_message_is_one_line_with_points_minute_time_and_link(self) -> None:
        record = CynicismMessageRecord(
            TARGET_MESSAGE_ID,
            CHANNEL_ID,
            datetime.datetime(2026, 7, 28, 23, 0, 45, tzinfo=datetime.UTC),
            "本文\n\n改行",
            (HUMAN_USER_ID, AUTHOR_USER_ID),
        )
        embeds = self.make_embeds([record])
        ensure(len(embeds) == 1)
        ensure(embeds[0].title == "発言者 の週間冷笑ポイント")
        ensure(
            embeds[0].description
            == (
                "2026-07-24 22:00 〜 2026-07-31 22:00 (JST)\n"
                f"- [07/29 08:00](https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{TARGET_MESSAGE_ID}) "
                f"「本文 改行」 **2pt** (名前付き、{AUTHOR_USER_ID})"
            )
        )
        ensure(embeds[0].footer.text is not None and "日時はJST" in embeds[0].footer.text)

    def test_all_messages_are_linked_but_long_content_is_abbreviated(self) -> None:
        records = [
            CynicismMessageRecord(i, CHANNEL_ID, datetime.datetime(2026, 7, 28, tzinfo=JST), "界" * 4500, (HUMAN_USER_ID,))
            for i in range(23)
        ]
        embeds = self.make_embeds(records)
        ensure(len(embeds) == 2)  # noqa: PLR2004 - 23件の抜粋は文字数上限で2ページに分かれる。
        ensure(all(len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT for embed in embeds))
        ensure(all(len(embed) <= 6000 for embed in embeds))  # noqa: PLR2004 - DiscordのEmbed上限。
        for current, following in pairwise(embeds):
            next_line = (following.description or "").splitlines()[1]
            ensure(len((current.description or "") + "\n" + next_line) > EMBED_DESCRIPTION_LIMIT)
        text = "\n".join(embed.description or "" for embed in embeds)
        ensure(text.count("界") == len(records) * MESSAGE_PREVIEW_LENGTH)
        ensure(text.count("…") == len(records))
        links = [f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{record.message_id})" for record in records]
        ensure(all(text.count(link) == 1 for link in links))
        ensure([text.index(link) for link in links] == sorted(text.index(link) for link in links))

    def test_markdown_heavy_content_stays_within_embed_limits(self) -> None:
        records = [
            CynicismMessageRecord(i, CHANNEL_ID, datetime.datetime(2026, 7, 28, tzinfo=JST), "*" * 500, (HUMAN_USER_ID,))
            for i in range(30)
        ]
        embeds = self.make_embeds(records)
        ensure(all(len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT for embed in embeds))
        ensure(sum((embed.description or "").count("[07/28 00:00]") for embed in embeds) == len(records))

    def test_empty_body_remains_readable(self) -> None:
        record = CynicismMessageRecord(1, CHANNEL_ID, datetime.datetime(2026, 7, 28, tzinfo=JST), "", (HUMAN_USER_ID,))
        description = self.make_embeds([record])[0].description or ""
        ensure("(本文なし)" in description and "1pt" in description)

    def test_empty_records_produce_no_embeds(self) -> None:
        ensure(self.make_embeds([]) == [])


class InProgressPeriodTest(unittest.TestCase):
    def test_embed_marks_a_period_that_has_not_ended_yet(self) -> None:
        ranking = build_sample_ranking()
        during_the_period = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=JST)

        embed = build_ranking_embed(ranking, updated_at=during_the_period)

        ensure(embed.title == "週間冷笑王 (集計中)")
        ensure(embed.description is not None and "途中経過" in embed.description)

    def test_embed_does_not_mark_a_completed_period(self) -> None:
        ranking = build_sample_ranking()
        after_the_period = datetime.datetime(2026, 7, 31, 22, 0, tzinfo=JST)

        embed = build_ranking_embed(ranking, updated_at=after_the_period)

        ensure(embed.title == "週間冷笑王")
        ensure(embed.description is not None and "途中経過" not in embed.description)


class PeriodDefaultTest(unittest.TestCase):
    """閲覧は進行中の期間、発表は確定した期間を既定にする。"""

    def build_use_cases(self) -> CynicismReportUseCases:
        return object.__new__(CynicismReportUseCases)

    def test_ranking_defaults_to_the_period_in_progress(self) -> None:
        use_cases = self.build_use_cases()
        now = datetime.datetime.now(JST)

        period = use_cases._resolve_period("weekly", None, default_to_completed=False)  # noqa: SLF001

        ensure(period.start_at <= now < period.end_at, "閲覧では現在を含む期間を既定にします")

    def test_publish_defaults_to_the_latest_completed_period(self) -> None:
        use_cases = self.build_use_cases()
        now = datetime.datetime.now(JST)

        period = use_cases._resolve_period("weekly", None, default_to_completed=True)  # noqa: SLF001

        ensure(period.end_at <= now, "発表では既に終わった期間を既定にします")


class InteractionRecorder:
    """DBアクセスと応答の順序を記録する、Interactionの代役。"""

    def __init__(self, user: object = None) -> None:
        self.calls: list[str] = []
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send_message)
        self.followup = SimpleNamespace(send=self._followup_send)
        self.user = user
        self.guild = None
        self.followup_kwargs: dict[str, object] | None = None
        self.defer_kwargs: dict[str, object] | None = None

    async def _defer(self, **kwargs: object) -> None:
        self.calls.append("defer")
        self.defer_kwargs = kwargs

    async def _send_message(self, *_: object, **__: object) -> None:
        self.calls.append("response.send_message")

    async def _followup_send(self, *_: object, **kwargs: object) -> None:
        self.calls.append("followup.send")
        self.followup_kwargs = kwargs

    async def record_database_access(self, *_: object, **__: object) -> object:
        self.calls.append("database")
        return make_settings()


def make_admin_member(role_id: int) -> discord.Member:
    """Bot管理者ロールを持つメンバーの代役を返します。"""
    member = Mock(spec=discord.Member)
    member.roles = [SimpleNamespace(id=role_id)]
    return member


class InteractionDeadlineTest(unittest.IsolatedAsyncioTestCase):
    """Discordの応答期限は3秒しかないため、DBアクセスの前に応答を保留する。"""

    ADMIN_ROLE_ID = 42

    def build_use_cases(self, interaction: InteractionRecorder) -> CynicismReportUseCases:
        use_cases = object.__new__(CynicismReportUseCases)
        use_cases._runtime_environment = cast("Any", SimpleNamespace(is_debug=True))  # noqa: SLF001
        use_cases._admin_role_id = self.ADMIN_ROLE_ID  # noqa: SLF001
        use_cases._target_id = 7  # noqa: SLF001
        use_cases._configuration = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(
                get=interaction.record_database_access,
                set_paused=interaction.record_database_access,
            ),
        )
        use_cases._reports = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(get_last_delivery=AsyncMock(return_value=None)),
        )
        return use_cases

    async def test_status_defers_before_touching_the_database(self) -> None:
        interaction = InteractionRecorder()
        use_cases = self.build_use_cases(interaction)

        await use_cases.status(cast("Any", interaction))

        ensure(interaction.calls[0] == "defer", "DBアクセスより先に応答を保留する必要があります")
        ensure("response.send_message" not in interaction.calls)
        ensure(interaction.calls[-1] == "followup.send")

    async def test_pause_and_resume_defer_before_touching_the_database(self) -> None:
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                interaction = InteractionRecorder(user=make_admin_member(self.ADMIN_ROLE_ID))
                use_cases = self.build_use_cases(interaction)

                await getattr(use_cases, action)(cast("Any", interaction))

                ensure(interaction.calls[0] == "defer", "DBアクセスより先に応答を保留する必要があります")
                ensure(interaction.calls[-1] == "followup.send")


class ShowMessagesTest(unittest.IsolatedAsyncioTestCase):
    """発言の紹介は、期間解決・チャンネル範囲判定・表示名解決を既存の実装に委譲する。"""

    def build_use_cases(
        self,
        *,
        records: list[CynicismMessageRecord] | None = None,
        display_names: dict[int, str] | None = None,
    ) -> CynicismReportUseCases:
        use_cases = object.__new__(CynicismReportUseCases)
        use_cases._runtime_environment = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(is_debug=False, chatbot_test_channel_ids=frozenset({TEST_CHANNEL_ID})),
        )
        use_cases._configuration = cast("Any", SimpleNamespace(get=AsyncMock(return_value=make_settings())))  # noqa: SLF001
        use_cases._reactions = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(
                list_member_reactions=AsyncMock(return_value=records or []),
                get_display_names=AsyncMock(return_value=display_names or {}),
            ),
        )
        bot = Mock()
        bot.user = SimpleNamespace(id=PAPYRUS_USER_ID)
        bot.get_user = Mock(
            side_effect=lambda member_id: (
                SimpleNamespace(display_name="Papyrus", bot=True) if member_id == PAPYRUS_USER_ID else None
            )
        )
        use_cases._bot = bot  # noqa: SLF001
        use_cases._server_id = GUILD_ID  # noqa: SLF001
        return use_cases

    def make_member(self, *, display_name: str = "被発言者") -> discord.Member:
        member = Mock(spec=discord.Member)
        member.id = AUTHOR_USER_ID
        member.display_name = display_name
        return member

    async def test_defers_before_touching_the_database(self) -> None:
        interaction = InteractionRecorder()
        use_cases = self.build_use_cases()

        await use_cases.show_messages(cast("Any", interaction), self.make_member(), "weekly", None)

        ensure(interaction.calls[0] == "defer", "DBアクセスより先に応答を保留する必要があります")
        ensure(interaction.calls[-1] == "followup.send")
        ensure(interaction.defer_kwargs == {"ephemeral": True, "thinking": True})

    async def test_no_records_sends_a_notice_without_a_file(self) -> None:
        interaction = InteractionRecorder()
        use_cases = self.build_use_cases(records=[])

        await use_cases.show_messages(cast("Any", interaction), self.make_member(), "weekly", None)

        ensure(interaction.followup_kwargs is not None)
        ensure("file" not in cast("dict[str, object]", interaction.followup_kwargs))
        ensure(cast("dict[str, object]", interaction.followup_kwargs)["ephemeral"] is True)

    async def test_sends_compact_embeds_with_inline_reactors_without_notifications(self) -> None:
        interaction = InteractionRecorder()
        send = AsyncMock(return_value=SimpleNamespace(id=99))
        interaction.followup.send = send
        records = [
            CynicismMessageRecord(
                i, CHANNEL_ID, datetime.datetime(2026, 7, 28, 21, tzinfo=JST), "@everyone " + "本文" * 1500, (HUMAN_USER_ID,)
            )
            for i in range(23)
        ]
        use_cases = self.build_use_cases(records=records, display_names={HUMAN_USER_ID: "人間さん"})
        await use_cases.show_messages(
            cast("Any", interaction), self.make_member(display_name="発言者さん"), "weekly", "2026-07-30"
        )
        ensure(send.await_count == 2)  # noqa: PLR2004 - 23件の抜粋は文字数上限で2ページに分かれる。
        for call in send.await_args_list:
            ensure(call.args == ())
            ensure(isinstance(call.kwargs["embed"], discord.Embed))
            ensure("file" not in call.kwargs and "files" not in call.kwargs)
            ensure(call.kwargs["ephemeral"] is True)
            ensure(call.kwargs["allowed_mentions"].to_dict() == {"parse": []})
            ensure(not call.kwargs.get("suppress_embeds"))
        text = "\n".join(call.kwargs["embed"].description for call in send.await_args_list)
        ensure("**1pt** (人間さん)" in text and "リアクター:" not in text)
        ensure("21:00:00" not in text)
        cast("Any", use_cases._reactions).get_display_names.assert_awaited_once_with([HUMAN_USER_ID])  # noqa: SLF001
        cast("Mock", use_cases._bot.dispatch).assert_not_called()  # noqa: SLF001


class ScheduledReportTest(unittest.IsolatedAsyncioTestCase):
    """対象が無い期間を毎分集計し直さないよう、処理済みとして記録する。"""

    TARGET_ID = 7

    def build_use_cases(
        self,
        *,
        deliveries: dict[tuple[str, datetime.date], SimpleNamespace] | None = None,
        ranking_is_empty: bool = True,
    ) -> tuple[CynicismReportUseCases, SimpleNamespace]:
        stored = deliveries if deliveries is not None else {}
        recorded = SimpleNamespace(empty_periods=[], posted_periods=[], aggregated=[])

        async def get_delivery(period: CynicismPeriod, _target_id: int) -> object | None:
            return stored.get((period.period_type.value, period.start_date))

        async def has_delivery(period: CynicismPeriod, target_id: int) -> bool:
            return await get_delivery(period, target_id) is not None

        async def save_empty(period: CynicismPeriod, _target_id: int, **_: object) -> None:
            recorded.empty_periods.append(period)

        async def save_posted(period: CynicismPeriod, _target_id: int, _message_id: int, **_: object) -> None:
            recorded.posted_periods.append(period)

        async def build_ranking_for(period: CynicismPeriod, **_: object) -> object:
            recorded.aggregated.append(period)
            return SimpleNamespace(is_empty=ranking_is_empty)

        use_cases = object.__new__(CynicismReportUseCases)
        use_cases._runtime_environment = cast("Any", SimpleNamespace(is_debug=False))  # noqa: SLF001
        use_cases._configuration = cast("Any", SimpleNamespace(get=AsyncMock(return_value=make_settings())))  # noqa: SLF001
        use_cases._reactions = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(earliest_recorded_date=AsyncMock(return_value=datetime.date(2026, 1, 1))),
        )
        use_cases._reports = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(
                get_delivery=get_delivery,
                has_delivery=has_delivery,
                save_empty=save_empty,
                save_posted=save_posted,
            ),
        )
        use_cases._target_id = self.TARGET_ID  # noqa: SLF001
        use_cases._report_lock = asyncio.Lock()  # noqa: SLF001
        use_cases.build_ranking_for = build_ranking_for  # type: ignore[method-assign]
        return use_cases, recorded

    async def test_empty_periods_are_recorded_as_processed(self) -> None:
        use_cases, recorded = self.build_use_cases()

        await use_cases.process_scheduled_reports(datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST))

        ensure(recorded.empty_periods, "投稿しなかった期間も処理済みとして記録する必要があります")
        ensure(not recorded.posted_periods)

    async def test_recorded_empty_periods_are_skipped_on_the_next_run(self) -> None:
        now = datetime.datetime(2026, 7, 31, 22, 30, tzinfo=JST)
        first_run, first_recorded = self.build_use_cases()
        await first_run.process_scheduled_reports(now)

        stored = {
            (period.period_type.value, period.start_date): SimpleNamespace(is_posted=False)
            for period in first_recorded.empty_periods
        }
        second_run, second_recorded = self.build_use_cases(deliveries=stored)
        await second_run.process_scheduled_reports(now)

        ensure(
            len(second_recorded.aggregated) < len(first_recorded.aggregated),
            "処理済みの期間は再集計しません",
        )
        ensure(not second_recorded.empty_periods)
