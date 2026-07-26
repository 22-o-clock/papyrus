import datetime
import unittest
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import discord

from cogs.cynicism.constants import CYNICISM_EMOJI, JST, REACTION_SOURCE, REPLY_SOURCE
from cogs.cynicism.models import (
    ChannelScope,
    CynicismSettings,
    CynicismWeights,
    MemberReactionCounts,
    RankedMemberIdentity,
)
from cogs.cynicism.periods import (
    CynicismPeriodType,
    format_period,
    latest_completed_period,
    period_containing,
    qualification_threshold,
)
from cogs.cynicism.services.message_delivery import (
    CynicismReportMessageDelivery,
    ReportMessageOwnershipError,
)
from cogs.cynicism.services.ranking import build_ranking, cynicism_rate, weighted_points
from cogs.cynicism.services.reaction_filter import is_cynicism_emoji, is_cynicism_only_content
from cogs.cynicism.services.report_builder import (
    build_empty_notice,
    build_ranking_embed,
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

DEFAULT_WEIGHTS = CynicismWeights(papyrus=Decimal("3.00"), human=Decimal("1.00"))
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


def ensure(condition: object, message: str = "") -> None:
    """条件を満たさない場合にテストを失敗させます。"""
    if not condition:
        raise AssertionError(message)


def make_settings(*, papyrus: str = "3.00", human: str = "1.00", is_paused: bool = False) -> CynicismSettings:
    """テスト用の運用設定を組み立てます。"""
    return CynicismSettings(
        weights=CynicismWeights(papyrus=Decimal(papyrus), human=Decimal(human)),
        is_paused=is_paused,
        paused_at=None,
    )


class PeriodTest(unittest.TestCase):
    def test_week_starts_on_monday(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(period.start_date == datetime.date(2026, 7, 20))
        ensure(period.end_date == datetime.date(2026, 7, 26))

    def test_week_spans_the_new_year(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 1, 1))

        ensure(period.start_date == datetime.date(2025, 12, 29))
        ensure(period.end_date == datetime.date(2026, 1, 4))

    def test_month_covers_the_last_day(self) -> None:
        period = period_containing(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(period.start_date == datetime.date(2026, 7, 1))
        ensure(period.end_date == datetime.date(2026, 7, 31))

    def test_february_of_a_leap_year_ends_on_the_29th(self) -> None:
        period = period_containing(CynicismPeriodType.MONTHLY, datetime.date(2024, 2, 10))

        ensure(period.end_date.day == FEBRUARY_LEAP_LAST_DAY)

    def test_year_covers_the_whole_calendar_year(self) -> None:
        period = period_containing(CynicismPeriodType.YEARLY, datetime.date(2026, 7, 26))

        ensure(period.start_date == datetime.date(2026, 1, 1))
        ensure(period.end_date == datetime.date(2026, 12, 31))

    def test_boundaries_are_jst_midnight_and_exclusive_at_the_end(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(period.start_at == datetime.datetime(2026, 7, 20, tzinfo=JST))
        ensure(period.end_at == datetime.datetime(2026, 7, 27, tzinfo=JST))

    def test_qualification_thresholds_match_the_agreed_values(self) -> None:
        target = datetime.date(2026, 7, 26)

        ensure(qualification_threshold(period_containing(CynicismPeriodType.WEEKLY, target)) == WEEKLY_THRESHOLD)
        ensure(qualification_threshold(period_containing(CynicismPeriodType.MONTHLY, target)) == MONTHLY_THRESHOLD)
        ensure(qualification_threshold(period_containing(CynicismPeriodType.YEARLY, target)) == YEARLY_THRESHOLD)

    def test_format_period_shows_the_inclusive_range(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(format_period(period) == "2026-07-20 〜 2026-07-26 (JST)")


class ScheduleTest(unittest.TestCase):
    def test_publish_time_is_the_day_after_the_period_at_21(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure(publish_time(period) == datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST))

    def test_period_is_not_published_before_its_publish_time(self) -> None:
        just_before = datetime.datetime(2026, 7, 27, 20, 59, tzinfo=JST)

        periods = publishable_periods(just_before, datetime.date(2026, 7, 20))

        ensure(all(period.start_date != datetime.date(2026, 7, 20) for period in periods))

    def test_period_is_published_once_its_publish_time_passes(self) -> None:
        just_after = datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST)

        periods = publishable_periods(just_after, datetime.date(2026, 7, 20))

        ensure(any(period.start_date == datetime.date(2026, 7, 20) for period in periods))

    def test_periods_before_the_first_record_are_skipped(self) -> None:
        now = datetime.datetime(2026, 7, 27, 22, 0, tzinfo=JST)

        periods = publishable_periods(now, datetime.date(2026, 7, 20))

        ensure(len(periods) == 1)
        ensure(periods[0].period_type is CynicismPeriodType.WEEKLY)

    def test_no_record_still_limits_backfill(self) -> None:
        now = datetime.datetime(2026, 7, 27, 22, 0, tzinfo=JST)

        periods = publishable_periods(now, None)

        weekly = [period for period in periods if period.period_type is CynicismPeriodType.WEEKLY]
        ensure(len(weekly) == 8)  # noqa: PLR2004 - MAXIMUM_BACKFILL_PERIODSの週次上限。

    def test_all_three_period_types_publish_when_the_year_starts_on_monday(self) -> None:
        # 2024-01-01は月曜のため、週次・月次・年次の発表時刻が同時に到来する。
        now = datetime.datetime(2024, 1, 1, 21, 30, tzinfo=JST)

        periods = publishable_periods(now, datetime.date(2023, 1, 1))
        published_types = {period.period_type for period in periods}

        ensure(len(published_types) == EXPECTED_PUBLISHABLE_PERIOD_COUNT)

    def test_publishable_periods_are_ordered_from_oldest(self) -> None:
        now = datetime.datetime(2026, 7, 27, 22, 0, tzinfo=JST)

        periods = publishable_periods(now, None)
        weekly = [period.start_date for period in periods if period.period_type is CynicismPeriodType.WEEKLY]

        ensure(weekly == sorted(weekly))

    def test_refreshable_periods_cover_recent_completed_periods(self) -> None:
        now = datetime.datetime(2026, 7, 27, 22, 0, tzinfo=JST)

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

    def test_cynicism_only_content_ignores_whitespace_and_variation_selector(self) -> None:
        for content in (CYNICISM_EMOJI, f"{CYNICISM_EMOJI}{CYNICISM_EMOJI}", f" {CYNICISM_EMOJI} ", f"{CYNICISM_EMOJI}️"):
            with self.subTest(content=content):
                ensure(is_cynicism_only_content(content))

    def test_content_with_other_characters_is_rejected(self) -> None:
        for content in (f"{CYNICISM_EMOJI}ですね", "", "   ", "冷笑"):
            with self.subTest(content=content):
                ensure(not is_cynicism_only_content(content))


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
    def test_papyrus_reactions_weigh_more_than_human_reactions(self) -> None:
        counts = MemberReactionCounts(member_id=1, papyrus_count=3, human_count=2, cynical_message_count=4)

        ensure(weighted_points(counts, DEFAULT_WEIGHTS) == Decimal("11.00"))

    def test_rate_is_zero_when_the_member_has_no_messages(self) -> None:
        ensure(cynicism_rate(Decimal(5), 0) == 0.0)

    def test_changing_the_weight_changes_the_total_champion(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, papyrus_count=1, human_count=0, cynical_message_count=1),
            MemberReactionCounts(member_id=2, papyrus_count=0, human_count=5, cynical_message_count=5),
        ]
        message_counts = {1: 50, 2: 50}
        identities = {
            1: RankedMemberIdentity(1, "少数精鋭", is_bot=False),
            2: RankedMemberIdentity(2, "数打ち", is_bot=False),
        }
        period = period_containing(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        with_default = build_ranking(period, counts, message_counts, identities, DEFAULT_WEIGHTS)
        with_heavier = build_ranking(
            period,
            counts,
            message_counts,
            identities,
            CynicismWeights(papyrus=Decimal("6.00"), human=Decimal("1.00")),
        )

        default_champion = with_default.total_champion
        heavier_champion = with_heavier.total_champion
        if default_champion is None or heavier_champion is None:
            self.fail("両方の重みで冷笑王が決まるはずです")
        ensure(default_champion.member_id == HEAVY_POSTER_ID, "既定の重みでは合計5.0ptの数打ちが1位になります")
        ensure(heavier_champion.member_id == LIGHT_POSTER_ID, "Papyrusの重みを6.0にすると合計6.0ptの少数精鋭が1位になります")

    def test_bot_authors_are_excluded_from_the_ranking(self) -> None:
        counts = [MemberReactionCounts(member_id=1, papyrus_count=1, human_count=0, cynical_message_count=1)]
        identities = {1: RankedMemberIdentity(1, "Bot", is_bot=True)}
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 100}, identities, DEFAULT_WEIGHTS)

        ensure(ranking.is_empty)

    def test_tied_totals_share_the_same_rank(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, papyrus_count=1, human_count=0, cynical_message_count=1),
            MemberReactionCounts(member_id=2, papyrus_count=1, human_count=0, cynical_message_count=1),
            MemberReactionCounts(member_id=3, papyrus_count=0, human_count=1, cynical_message_count=1),
        ]
        identities = {
            1: RankedMemberIdentity(1, "A", is_bot=False),
            2: RankedMemberIdentity(2, "B", is_bot=False),
            3: RankedMemberIdentity(3, "C", is_bot=False),
        }
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 20, 2: 20, 3: 20}, identities, DEFAULT_WEIGHTS)

        ensure([entry.rank for entry in ranking.total_entries] == [1, 1, 3])

    def test_members_below_the_threshold_stay_in_the_total_ranking_only(self) -> None:
        counts = [
            MemberReactionCounts(member_id=1, papyrus_count=1, human_count=0, cynical_message_count=1),
            MemberReactionCounts(member_id=2, papyrus_count=1, human_count=0, cynical_message_count=1),
        ]
        identities = {
            1: RankedMemberIdentity(1, "常連", is_bot=False),
            2: RankedMemberIdentity(2, "一言だけ", is_bot=False),
        }
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 40, 2: 1}, identities, DEFAULT_WEIGHTS)

        ensure({entry.member_id for entry in ranking.total_entries} == {1, 2})
        ensure([entry.member_id for entry in ranking.rate_entries] == [1])
        ensure(ranking.qualified_member_count == 1)

    def test_summary_counts_reactions_by_source(self) -> None:
        counts = [MemberReactionCounts(member_id=1, papyrus_count=2, human_count=3, cynical_message_count=4)]
        identities = {1: RankedMemberIdentity(1, "A", is_bot=False)}
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ranking = build_ranking(period, counts, {1: 20}, identities, DEFAULT_WEIGHTS)

        ensure(ranking.papyrus_reaction_count == 2)  # noqa: PLR2004 - テストデータの件数。
        ensure(ranking.human_reaction_count == 3)  # noqa: PLR2004 - テストデータの件数。
        ensure(ranking.total_points == Decimal("9.00"))


def build_sample_ranking(*, papyrus_weight: str = "3.00") -> "CynicismRanking":
    """Embed・digestのテストで使う代表的なランキングを返します。"""
    counts = [
        MemberReactionCounts(member_id=1, papyrus_count=3, human_count=1, cynical_message_count=3),
        MemberReactionCounts(member_id=2, papyrus_count=0, human_count=2, cynical_message_count=2),
    ]
    identities = {
        1: RankedMemberIdentity(1, "冷笑家", is_bot=False),
        2: RankedMemberIdentity(2, "ときどき", is_bot=False),
    }
    period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
    weights = CynicismWeights(papyrus=Decimal(papyrus_weight), human=Decimal("1.00"))
    return build_ranking(period, counts, {1: 40, 2: 30}, identities, weights)


class ReportBuilderTest(unittest.TestCase):
    def test_marker_identifies_the_period_type_and_start(self) -> None:
        period = period_containing(CynicismPeriodType.MONTHLY, datetime.date(2026, 7, 15))

        ensure(report_marker(period) == "cynicism-report:monthly:2026-07-01")

    def test_embed_shows_both_champions_and_the_threshold(self) -> None:
        ranking = build_sample_ranking()

        embed = build_ranking_embed(ranking, updated_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST))

        ensure(embed.title == "週間冷笑王")
        ensure(embed.description == "2026-07-20 〜 2026-07-26 (JST)")
        field_names = [field.name for field in embed.fields]
        ensure(any(name is not None and "冷笑王 (合計)" in name for name in field_names))
        ensure(any(name is not None and "冷笑率王 (平均)" in name for name in field_names))
        footer_text = embed.footer.text
        if footer_text is None:
            self.fail("フッターに識別子と重みが必要です")
        ensure(report_marker(ranking.period) in footer_text)
        ensure("重み Papyrus 3.0 / 人間 1.0" in footer_text)

    def test_rate_champion_is_absent_when_nobody_qualifies(self) -> None:
        counts = [MemberReactionCounts(member_id=1, papyrus_count=1, human_count=0, cynical_message_count=1)]
        identities = {1: RankedMemberIdentity(1, "一言だけ", is_bot=False)}
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))
        ranking = build_ranking(period, counts, {1: 1}, identities, DEFAULT_WEIGHTS)

        embed = build_ranking_embed(ranking, updated_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST))

        rate_field = next(field for field in embed.fields if field.name is not None and "冷笑率王" in field.name)
        ensure(rate_field.value is not None and "該当なし" in rate_field.value)

    def test_digest_changes_when_the_weight_changes(self) -> None:
        ensure(ranking_digest(build_sample_ranking()) != ranking_digest(build_sample_ranking(papyrus_weight="5.00")))

    def test_digest_is_stable_for_the_same_ranking(self) -> None:
        ensure(ranking_digest(build_sample_ranking()) == ranking_digest(build_sample_ranking()))

    def test_empty_notice_names_the_period(self) -> None:
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        ensure("2026-07-20 〜 2026-07-26 (JST)" in build_empty_notice(period))


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
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        result = await delivery.upsert(period, discord.Embed(), "digest")

        ensure(result.changed)
        target.send.assert_awaited_once()
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
            last_updated_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

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
            last_updated_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        result = await delivery.upsert(period, discord.Embed(), "new")

        ensure(result.changed)
        message.edit.assert_awaited_once()
        bot.dispatch.assert_called_once()

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
            last_updated_at=datetime.datetime(2026, 7, 27, 21, 0, tzinfo=JST),
        )
        repository = cast("Any", SimpleNamespace(get_delivery=AsyncMock(return_value=stored)))
        delivery = CynicismReportMessageDelivery(bot, repository, target_id=7)
        period = period_containing(CynicismPeriodType.WEEKLY, datetime.date(2026, 7, 26))

        try:
            await delivery.upsert(period, discord.Embed(), "new")
        except ReportMessageOwnershipError:
            message.edit.assert_not_awaited()
            return
        self.fail("別Botの投稿は上書きせず設定エラーにする必要があります")


def make_tracking_use_cases(
    *,
    is_paused: bool = False,
    target_message: object | None = None,
    is_debug: bool = False,
) -> tuple[CynicismTrackingUseCases, Any, Any]:
    """記録用ユースケースと、注入したBot・リポジトリの代役を返します。"""
    channel = Mock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(
        return_value=target_message
        or SimpleNamespace(
            author=SimpleNamespace(id=AUTHOR_USER_ID, bot=False),
            created_at=datetime.datetime(2026, 7, 22, 3, 0, tzinfo=datetime.UTC),
        )
    )
    bot = cast(
        "Any",
        SimpleNamespace(
            user=SimpleNamespace(id=PAPYRUS_USER_ID),
            get_channel=Mock(return_value=channel),
            get_user=Mock(return_value=None),
            get_guild=Mock(return_value=None),
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
            remove_reply_evidence=AsyncMock(),
        ),
    )
    configuration = cast("Any", SimpleNamespace(get=AsyncMock(return_value=make_settings(is_paused=is_paused))))
    return CynicismTrackingUseCases(bot, runtime, reactions, configuration), bot, reactions


def make_reaction_payload(
    *,
    emoji_name: str = CYNICISM_EMOJI,
    channel_id: int = CHANNEL_ID,
) -> discord.RawReactionActionEvent:
    """リアクションイベントの代役を返します。"""
    return cast(
        "discord.RawReactionActionEvent",
        SimpleNamespace(
            emoji=discord.PartialEmoji(name=emoji_name),
            guild_id=GUILD_ID,
            channel_id=channel_id,
            message_id=12345,
            user_id=HUMAN_USER_ID,
            message_author_id=AUTHOR_USER_ID,
            member=SimpleNamespace(bot=False),
            burst=False,
        ),
    )


def make_reply_message(
    *,
    author_id: int = PAPYRUS_USER_ID,
    content: str = CYNICISM_EMOJI,
    reference: bool = True,
) -> discord.Message:
    """🥶だけの返信メッセージの代役を返します。"""
    return cast(
        "discord.Message",
        SimpleNamespace(
            id=777,
            author=SimpleNamespace(id=author_id),
            content=content,
            reference=SimpleNamespace(message_id=12345) if reference else None,
            guild=SimpleNamespace(id=GUILD_ID),
            channel=SimpleNamespace(id=CHANNEL_ID),
        ),
    )


class TrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cynicism_reaction_is_recorded(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_awaited_once()
        event = reactions.record.await_args.args[0]
        ensure(event.source == REACTION_SOURCE)
        ensure(event.message_author_id == AUTHOR_USER_ID)
        ensure(event.message_posted_at.tzinfo is not None)

    async def test_other_emoji_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload(emoji_name="😀"))

        reactions.record.assert_not_awaited()

    async def test_reaction_outside_the_target_channel_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_reaction_add(make_reaction_payload(channel_id=TEST_CHANNEL_ID))

        reactions.record.assert_not_awaited()

    async def test_reaction_on_a_bot_message_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(
            target_message=SimpleNamespace(
                author=SimpleNamespace(id=PAPYRUS_USER_ID, bot=True),
                created_at=datetime.datetime(2026, 7, 22, 3, 0, tzinfo=datetime.UTC),
            )
        )

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_not_awaited()

    async def test_paused_tracking_records_nothing(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(is_paused=True)

        await use_cases.on_reaction_add(make_reaction_payload())

        reactions.record.assert_not_awaited()

    async def test_paused_tracking_still_removes_cancelled_reactions(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases(is_paused=True)

        await use_cases.on_reaction_remove(make_reaction_payload())

        reactions.remove_reaction.assert_awaited_once()

    async def test_papyrus_reply_with_only_the_emoji_is_recorded(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_message(make_reply_message())

        reactions.record.assert_awaited_once()
        event = reactions.record.await_args.args[0]
        ensure(event.source == REPLY_SOURCE)
        ensure(event.evidence_message_id == 777)  # noqa: PLR2004 - テストデータの返信メッセージID。
        ensure(event.reactor_id == PAPYRUS_USER_ID)
        ensure(event.reactor_is_bot)

    async def test_reply_with_extra_text_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_message(make_reply_message(content=f"{CYNICISM_EMOJI} ですね"))

        reactions.record.assert_not_awaited()

    async def test_standalone_emoji_post_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_message(make_reply_message(reference=False))

        reactions.record.assert_not_awaited()

    async def test_reply_from_someone_else_is_ignored(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_message(make_reply_message(author_id=HUMAN_USER_ID))

        reactions.record.assert_not_awaited()

    async def test_deleting_the_reply_removes_its_point(self) -> None:
        use_cases, _, reactions = make_tracking_use_cases()

        await use_cases.on_raw_message_delete(cast("Any", SimpleNamespace(message_id=777)))

        reactions.remove_reply_evidence.assert_awaited_once_with(777)


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
            use_cases._resolve_period("daily", None)  # noqa: SLF001
        except ArgumentError:
            return
        self.fail("未知の期間種別は利用者向けエラーにする必要があります")

    def test_malformed_start_date_is_rejected(self) -> None:
        use_cases = self.build_use_cases()

        try:
            use_cases._resolve_period("weekly", "2026/07/20")  # noqa: SLF001
        except ArgumentError:
            return
        self.fail("開始日の書式誤りは利用者向けエラーにする必要があります")

    def test_start_date_is_normalized_to_the_containing_period(self) -> None:
        use_cases = self.build_use_cases()

        period = use_cases._resolve_period("weekly", "2026-07-23")  # noqa: SLF001

        ensure(period.start_date == datetime.date(2026, 7, 20))

    def test_weight_outside_the_allowed_range_is_rejected(self) -> None:
        use_cases = self.build_use_cases()

        for weight in (-1.0, 1000.0):
            with self.subTest(weight=weight):
                try:
                    use_cases._validate_weight(weight)  # noqa: SLF001
                except ArgumentError:
                    continue
                self.fail("許容範囲外の重みは利用者向けエラーにする必要があります")

    def test_weight_inside_the_allowed_range_is_accepted(self) -> None:
        use_cases = self.build_use_cases()

        ensure(use_cases._validate_weight(2.5) == Decimal("2.5"))  # noqa: SLF001
        ensure(use_cases._validate_weight(None) is None)  # noqa: SLF001


class ChannelScopeModelTest(unittest.TestCase):
    def test_included_ids_take_precedence(self) -> None:
        scope = ChannelScope(included_channel_ids=frozenset({1}), excluded_channel_ids=frozenset({1}))

        ensure(scope.contains(1))
        ensure(not scope.contains(2))
