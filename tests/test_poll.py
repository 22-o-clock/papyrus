"""秘密投票の保存・集計、入力の解析、表示を検証する。"""

import datetime
import unittest
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
from sqlalchemy import Table, create_engine, select

from cogs.poll.constants import JST, REOPEN_WINDOW
from cogs.poll.database import Poll, PollBallot, PollElectorate
from cogs.poll.models import BallotOutcome, PollRecord, PollStatus
from cogs.poll.repositories.poll import PollRepository
from cogs.poll.services.ballot_key import make_ballot_key
from cogs.poll.services.duration import parse_deadline
from cogs.poll.services.presentation import build_electorate_embed, build_poll_embed, build_result_embed
from cogs.poll.services.validation import parse_options, parse_question
from cogs.poll.use_cases.polls import PollUseCases
from cogs.poll.views import build_poll_view, build_result_view
from core.exception import ArgumentError

GUILD_ID = 1
CHANNEL_ID = 2
CREATOR_ID = 3
VOTER_ID = 10
OTHER_VOTER_ID = 11
OUTSIDER_ID = 99
ROLE_ID = 5
TARGET_ROLE_ID = 6
SECRET = b"0123456789abcdef0123456789abcdef"
# SQLiteは時差を保持しないため、保存と比較にはタイムゾーンなしの時刻を使う。
NOW = datetime.datetime(2026, 9, 26, 12, 0)  # noqa: DTZ001


EXPECTED_VOTERS = 2
OPEN_BUTTON_COUNT = 2
PREVIOUS_RESULT_ID = 7
EMBED_DESCRIPTION_LIMIT = 4096


def ensure(condition: object, message: str = "") -> None:
    """条件が成立しなければ検証を失敗させる。"""
    if not condition:
        raise AssertionError(message)


def require[T](value: T | None) -> T:
    """値がNoneなら検証を失敗させ、そうでなければ返す。"""
    if value is None:
        raise AssertionError
    return value


def ensure_argument_error(operation: Callable[[], object]) -> None:
    """操作がArgumentErrorを送出しなければ検証を失敗させる。"""
    try:
        operation()
    except ArgumentError:
        return
    raise AssertionError


class SqliteDatabase:
    """秘密投票のSQLを実行し、セッション終了時にコミットするテスト用DB。"""

    def __init__(self) -> None:
        """独立したメモリDBへ必要なテーブルを作る。"""
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS talkdata")
            for model in (Poll, PollBallot, PollElectorate):
                cast("Table", model.__table__).create(connection)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SimpleNamespace]:
        """SQLAlchemyの同期接続をリポジトリの非同期インターフェースへ適合させる。"""
        with self.engine.begin() as connection:
            yield SimpleNamespace(
                execute=AsyncMock(side_effect=connection.execute), scalar=AsyncMock(side_effect=connection.scalar)
            )


class PollRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    """投票を1件作成した状態から検証する。"""

    allow_change = True

    async def asyncSetUp(self) -> None:
        self.database = SqliteDatabase()
        self.addCleanup(self.database.engine.dispose)
        self.repository = PollRepository(cast("Any", self.database))
        self.poll = await self.repository.create(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            creator_id=CREATOR_ID,
            question="昼食",
            options=("カレー", "うどん", "そば"),
            allow_multiple=True,
            allow_change=self.allow_change,
            target_role_id=ROLE_ID,
            closes_at=NOW + datetime.timedelta(hours=1),
            electorate_ids=(VOTER_ID, OTHER_VOTER_ID),
        )

    def key(self, user_id: int) -> str:
        """投票者の秘匿キーを返す。"""
        return make_ballot_key(SECRET, self.poll.id, user_id)

    async def cast(self, user_id: int, choices: list[int], now: datetime.datetime = NOW) -> BallotOutcome:
        """投票し、結果を返す。"""
        result = await self.repository.cast_ballot(self.poll.id, user_id, self.key(user_id), choices, now)
        return result.outcome

    def stored_ballots(self) -> list[tuple[str, Any]]:
        """保存された票の行を返す。"""
        with self.database.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(select(PollBallot.voter_key, PollBallot.choices))]


class ChangeablePollRepositoryTest(PollRepositoryTestCase):
    """変更可モードの投票、変更、取り消し、締め切り、再開、確定を確認する。"""

    async def test_ballots_are_stored_without_user_ids(self) -> None:
        ensure(await self.cast(VOTER_ID, [0, 2]) is BallotOutcome.ACCEPTED)

        ballots = self.stored_ballots()
        ensure(ballots == [(self.key(VOTER_ID), [0, 2])])
        ensure(str(VOTER_ID) not in ballots[0][0])

    async def test_changing_a_vote_overwrites_the_previous_ballot(self) -> None:
        await self.cast(VOTER_ID, [0])
        await self.cast(VOTER_ID, [1])

        ensure(await self.repository.get_choices(self.poll.id, self.key(VOTER_ID)) == (1,))
        ensure(await self.repository.count_voters(self.poll.id) == 1)

    async def test_withdrawal_removes_the_ballot(self) -> None:
        await self.cast(VOTER_ID, [0])
        result = await self.repository.withdraw_ballot(self.poll.id, self.key(VOTER_ID), NOW)

        ensure(result.outcome is BallotOutcome.ACCEPTED)
        ensure(result.voter_count == 0)
        ensure(await self.repository.get_choices(self.poll.id, self.key(VOTER_ID)) is None)

    async def test_only_the_snapshotted_electorate_can_vote(self) -> None:
        ensure(await self.cast(OUTSIDER_ID, [0]) is BallotOutcome.NOT_ELIGIBLE)
        ensure(self.stored_ballots() == [])
        ensure(await self.repository.list_electorate(self.poll.id) == [VOTER_ID, OTHER_VOTER_ID])

    async def test_votes_after_the_deadline_are_rejected_before_closing(self) -> None:
        late = NOW + datetime.timedelta(hours=1)

        ensure(await self.cast(VOTER_ID, [0], now=late) is BallotOutcome.NOT_OPEN)

    async def test_close_publishes_counts_and_keeps_ballots_until_finalized(self) -> None:
        await self.cast(VOTER_ID, [0, 1])
        await self.cast(OTHER_VOTER_ID, [1])

        closed = require(await self.repository.close(self.poll.id, NOW))

        ensure(closed.status is PollStatus.CLOSED)
        ensure(closed.result_counts == (1, 2, 0))
        ensure(closed.result_voter_count == EXPECTED_VOTERS)
        ensure(len(self.stored_ballots()) == EXPECTED_VOTERS)
        ensure(await self.repository.close(self.poll.id, NOW) is None)

    async def test_reopen_continues_the_same_ballots(self) -> None:
        await self.cast(VOTER_ID, [0])
        await self.repository.close(self.poll.id, NOW)
        reopened = require(await self.repository.reopen(self.poll.id, NOW, NOW + datetime.timedelta(hours=2)))

        ensure(reopened.status is PollStatus.OPEN)
        ensure(reopened.result_counts == (1, 0, 0))
        ensure(await self.cast(VOTER_ID, [2]) is BallotOutcome.ACCEPTED)
        closed = require(await self.repository.close(self.poll.id, NOW))
        ensure(closed.result_counts == (0, 0, 1))

    async def test_reopen_is_rejected_after_the_window(self) -> None:
        await self.repository.close(self.poll.id, NOW)

        ensure(await self.repository.reopen(self.poll.id, NOW + REOPEN_WINDOW, NOW + REOPEN_WINDOW * 2) is None)

    async def test_finalize_deletes_ballots_only_after_the_window(self) -> None:
        await self.cast(VOTER_ID, [0])
        await self.repository.close(self.poll.id, NOW)

        ensure(await self.repository.finalize(self.poll.id, NOW) is None)
        ensure(await self.repository.list_due_to_finalize(NOW) == [])
        ensure(await self.repository.list_due_to_finalize(NOW + REOPEN_WINDOW) == [self.poll.id])
        finalized = require(await self.repository.finalize(self.poll.id, NOW + REOPEN_WINDOW))

        ensure(finalized.status is PollStatus.FINALIZED)
        ensure(finalized.result_counts == (1, 0, 0))
        ensure(self.stored_ballots() == [])
        ensure(await self.repository.list_electorate(self.poll.id) == [VOTER_ID, OTHER_VOTER_ID])

    async def test_due_polls_are_listed_at_the_deadline(self) -> None:
        ensure(await self.repository.list_due_to_close(NOW) == [])
        ensure(await self.repository.list_due_to_close(NOW + datetime.timedelta(hours=1)) == [self.poll.id])


class FixedPollRepositoryTest(PollRepositoryTestCase):
    """変更不可モードで、票の行に選択を残さないことを確認する。"""

    allow_change = False

    async def test_choices_are_only_added_to_the_poll_counts(self) -> None:
        ensure(await self.cast(VOTER_ID, [0, 2]) is BallotOutcome.ACCEPTED)

        ensure(self.stored_ballots() == [(self.key(VOTER_ID), None)])
        poll = require(await self.repository.get(self.poll.id))
        ensure(poll.live_counts == (1, 0, 1))

    async def test_second_vote_and_withdrawal_are_rejected(self) -> None:
        await self.cast(VOTER_ID, [0])

        ensure(await self.cast(VOTER_ID, [1]) is BallotOutcome.ALREADY_VOTED)
        withdrawal = await self.repository.withdraw_ballot(self.poll.id, self.key(VOTER_ID), NOW)
        ensure(withdrawal.outcome is BallotOutcome.UNCHANGEABLE)
        poll = require(await self.repository.get(self.poll.id))
        ensure(poll.live_counts == (1, 0, 0))

    async def test_close_and_finalize_use_the_added_counts(self) -> None:
        await self.cast(VOTER_ID, [0])
        await self.cast(OTHER_VOTER_ID, [0, 1])

        closed = require(await self.repository.close(self.poll.id, NOW))
        ensure(closed.result_counts == (2, 1, 0))
        ensure(closed.result_voter_count == EXPECTED_VOTERS)
        finalized = require(await self.repository.finalize(self.poll.id, NOW + REOPEN_WINDOW))
        ensure(finalized.live_counts is None)
        ensure(self.stored_ballots() == [])


class BallotKeyTest(unittest.TestCase):
    def test_key_depends_on_the_secret_and_the_poll(self) -> None:
        key = make_ballot_key(SECRET, 1, VOTER_ID)

        ensure(key == make_ballot_key(SECRET, 1, VOTER_ID))
        ensure(key != make_ballot_key(SECRET, 2, VOTER_ID))
        ensure(key != make_ballot_key(b"another-secret-another-secret-00", 1, VOTER_ID))
        ensure(str(VOTER_ID) not in key)


class DeadlineTest(unittest.TestCase):
    now = datetime.datetime(2026, 9, 26, 21, 30, tzinfo=JST)

    def test_relative_durations(self) -> None:
        ensure(parse_deadline("24h", self.now) == self.now + datetime.timedelta(hours=24))
        ensure(parse_deadline("1d 12h", self.now) == self.now + datetime.timedelta(days=1, hours=12))
        ensure(parse_deadline("30m", self.now) == self.now + datetime.timedelta(minutes=30))

    def test_time_only_moves_to_the_next_day_when_passed(self) -> None:
        ensure(parse_deadline("22:00", self.now) == datetime.datetime(2026, 9, 26, 22, 0, tzinfo=JST))
        ensure(parse_deadline("21:00", self.now) == datetime.datetime(2026, 9, 27, 21, 0, tzinfo=JST))

    def test_past_date_moves_to_the_next_year(self) -> None:
        ensure(parse_deadline("09/30 21:00", self.now) == datetime.datetime(2026, 9, 30, 21, 0, tzinfo=JST))
        ensure(parse_deadline("01/05 09:00", self.now) == datetime.datetime(2027, 1, 5, 9, 0, tzinfo=JST))

    def test_invalid_values_are_rejected(self) -> None:
        for value in ("", "abc", "0m", "02/30 10:00", "25:00"):
            with self.subTest(value=value):
                ensure_argument_error(lambda value=value: parse_deadline(value, self.now))


class ValidationTest(unittest.TestCase):
    def test_options_ignore_blank_lines(self) -> None:
        ensure(parse_options(" 賛成 \n\n反対\n") == ("賛成", "反対"))

    def test_invalid_options_are_rejected(self) -> None:
        for value in ("賛成", "賛成\n賛成", "\n".join(str(index) for index in range(26)), f"{'a' * 101}\nb"):
            with self.subTest(value=value[:20]):
                ensure_argument_error(lambda value=value: parse_options(value))

    def test_blank_question_is_rejected(self) -> None:
        ensure_argument_error(lambda: parse_question("  "))


def make_record(**overrides: object) -> PollRecord:
    """表示の検証用に投票を作る。"""
    values: dict[str, Any] = {
        "id": 1,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "message_id": 4,
        "result_message_id": None,
        "creator_id": CREATOR_ID,
        "question": "昼食",
        "options": ("カレー", "うどん"),
        "allow_multiple": False,
        "allow_change": True,
        "target_role_id": ROLE_ID,
        "closes_at": datetime.datetime(2026, 9, 27, 12, 0, tzinfo=JST),
        "closed_at": None,
        "finalized_at": None,
        "live_counts": None,
        "result_counts": None,
        "result_voter_count": None,
    }
    values.update(overrides)
    return PollRecord(**values)


def embed_text(embed: discord.Embed) -> str:
    """Embedの本文とフィールドを連結する。"""
    return "\n".join([embed.description or "", *(f"{field.name}: {field.value}" for field in embed.fields)])


class PresentationTest(unittest.TestCase):
    def test_open_poll_shows_only_counts_of_people(self) -> None:
        text = embed_text(build_poll_embed(make_record(live_counts=(3, 1)), 4, 10, ROLE_ID))

        ensure("票" not in (build_poll_embed(make_record(), 4, 10, ROLE_ID).description or ""))
        ensure("4人 / 有権者10人" in text)
        ensure(f"作成時点で<@&{ROLE_ID}>を持っていたメンバー" in text)

    def test_target_role_is_shown_together_with_the_electorate_role(self) -> None:
        text = embed_text(build_poll_embed(make_record(target_role_id=TARGET_ROLE_ID), 0, 10, ROLE_ID))

        ensure(f"<@&{TARGET_ROLE_ID}>と<@&{ROLE_ID}>の両方" in text)

    def test_closed_announcement_links_to_the_result_without_counts(self) -> None:
        closed_at = datetime.datetime(2026, 9, 27, 12, 0, tzinfo=JST)
        poll = make_record(closed_at=closed_at, result_counts=(3, 1), result_voter_count=4, result_message_id=7)

        text = embed_text(build_poll_embed(poll, 0, 10, ROLE_ID))
        ensure("票" not in text.replace("投票", ""))
        ensure(f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/7" in text)
        ensure("4人 / 有権者10人" in text)

    def test_result_message_shows_counts_and_ratio(self) -> None:
        closed_at = datetime.datetime(2026, 9, 27, 12, 0, tzinfo=JST)
        poll = make_record(closed_at=closed_at, result_counts=(3, 1), result_voter_count=4)

        embed = build_result_embed(poll, 10, ROLE_ID)
        ensure((embed.title or "").startswith("投票結果"))
        ensure("**3票**(75%)" in (embed.description or ""))
        ensure("**1票**(25%)" in (embed.description or ""))
        finalized = make_record(closed_at=closed_at, finalized_at=closed_at, result_counts=(3, 1), result_voter_count=4)
        ensure((build_result_embed(finalized, 10, ROLE_ID).title or "").startswith("確定結果"))

    def test_reopened_poll_links_to_the_previous_result(self) -> None:
        poll = make_record(result_counts=(3, 1), result_voter_count=4, result_message_id=7)

        embed = build_poll_embed(poll, 5, 10, ROLE_ID)
        ensure("票" not in (embed.description or ""))
        ensure(any(field.name == "前回の締め切り時点の結果" for field in embed.fields))
        ensure("再開しました" in embed_text(build_result_embed(poll, 10, ROLE_ID)))

    def test_buttons_follow_the_status(self) -> None:
        closed_at = datetime.datetime(2026, 9, 27, 12, 0, tzinfo=JST)
        closed = make_record(closed_at=closed_at, result_message_id=7)

        ensure(len(require(build_poll_view(make_record())).children) == OPEN_BUTTON_COUNT)
        ensure(build_poll_view(closed) is None)
        ensure(build_result_view(closed) is not None)
        ensure(build_poll_view(make_record(closed_at=closed_at)) is not None)
        ensure(build_result_view(make_record(closed_at=closed_at, finalized_at=closed_at)) is None)

    def test_large_electorate_is_attached(self) -> None:
        embed, needs_attachment = build_electorate_embed(make_record(), [10**17 + index for index in range(300)], ROLE_ID)

        ensure(needs_attachment)
        ensure(len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT)


class TargetRoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.use_cases = PollUseCases(Mock(), Mock(), ballot_secret=SECRET, electorate_role_id=ROLE_ID)

    def test_default_is_the_electorate_role(self) -> None:
        electorate = Mock(is_default=Mock(return_value=False))
        guild = Mock(get_role=Mock(return_value=electorate))

        ensure(self.use_cases._resolve_roles(guild, None) == (electorate, electorate))  # noqa: SLF001
        guild.get_role.assert_called_once_with(ROLE_ID)

    def test_specified_role_still_requires_the_electorate_role(self) -> None:
        electorate = Mock(is_default=Mock(return_value=False))
        target = Mock(is_default=Mock(return_value=False))

        roles = self.use_cases._resolve_roles(Mock(get_role=Mock(return_value=electorate)), target)  # noqa: SLF001
        ensure(roles == (target, electorate))
        missing = Mock(get_role=Mock(return_value=None))
        ensure_argument_error(lambda: self.use_cases._resolve_roles(missing, target))  # noqa: SLF001

    def test_everyone_is_rejected(self) -> None:
        everyone = Mock(is_default=Mock(return_value=True))
        guild = Mock(get_role=Mock(return_value=Mock(is_default=Mock(return_value=False))))
        ensure_argument_error(lambda: self.use_cases._resolve_roles(guild, everyone))  # noqa: SLF001


def make_member(user_id: int, *, has_electorate_role: bool, bot: bool = False) -> Mock:
    """有権者ロールの有無を指定したメンバーを作る。"""
    return Mock(id=user_id, bot=bot, get_role=Mock(return_value=Mock() if has_electorate_role else None))


class CreatePollTest(unittest.IsolatedAsyncioTestCase):
    """作成時点の有権者を記録し、告知メッセージを投稿することを確認する。"""

    def setUp(self) -> None:
        self.repository = AsyncMock()
        self.repository.create.return_value = make_record(message_id=None)
        self.use_cases = PollUseCases(Mock(), self.repository, ballot_secret=SECRET, electorate_role_id=ROLE_ID)
        self.electorate_role = Mock(id=ROLE_ID, mention=f"<@&{ROLE_ID}>")
        members = [
            make_member(VOTER_ID, has_electorate_role=True),
            make_member(OTHER_VOTER_ID, has_electorate_role=True),
            make_member(OUTSIDER_ID, has_electorate_role=True, bot=True),
            make_member(OUTSIDER_ID + 1, has_electorate_role=False),
        ]
        self.role = Mock(id=TARGET_ROLE_ID, members=members, mention=f"<@&{TARGET_ROLE_ID}>")
        self.interaction = Mock(guild=Mock(id=GUILD_ID, chunked=True), channel_id=CHANNEL_ID, user=Mock(id=CREATOR_ID))
        self.interaction.response.defer = AsyncMock()
        self.interaction.followup.send = AsyncMock(return_value=Mock(id=4))

    async def create(self, options: str = "賛成\n反対") -> None:
        """フォームの入力で投票を作成する。"""
        await self.use_cases._create(  # noqa: SLF001
            self.interaction,
            "議題",
            options,
            duration="24h",
            allow_multiple=False,
            allow_change=False,
            role=self.role,
            electorate_role=self.electorate_role,
        )

    async def test_electorate_requires_both_roles_and_excludes_bots(self) -> None:
        await self.create()

        arguments = self.repository.create.await_args.kwargs
        ensure(arguments["electorate_ids"] == [VOTER_ID, OTHER_VOTER_ID])
        ensure(arguments["allow_change"] is False)
        ensure(arguments["target_role_id"] == TARGET_ROLE_ID)
        self.interaction.response.defer.assert_awaited_once()
        self.repository.attach_message.assert_awaited_once_with(1, 4)

    async def test_invalid_input_is_rejected_before_responding(self) -> None:
        try:
            await self.create(options="賛成")
        except ArgumentError:
            self.interaction.response.defer.assert_not_awaited()
            self.repository.create.assert_not_awaited()
            return
        raise AssertionError

    async def test_poll_is_removed_when_the_announcement_fails(self) -> None:
        self.interaction.followup.send.side_effect = discord.HTTPException(Mock(status=500, reason=""), "")

        try:
            await self.create()
        except discord.HTTPException:
            self.repository.delete.assert_awaited_once_with(1)
            self.repository.attach_message.assert_not_awaited()
            return
        raise AssertionError


class StatusChangeMessageTest(unittest.IsolatedAsyncioTestCase):
    """締め切り、再開、確定を、告知メッセージへの返信として新しく投稿することを確認する。"""

    def setUp(self) -> None:
        self.repository = AsyncMock()
        self.repository.count_electorate.return_value = 10
        self.channel = Mock(send=AsyncMock(return_value=Mock(id=8)))
        self.previous_result = Mock(edit=AsyncMock())
        self.announcement = Mock(edit=AsyncMock())
        self.channel.get_partial_message = Mock(
            side_effect=lambda message_id: self.previous_result if message_id == PREVIOUS_RESULT_ID else self.announcement
        )
        bot = Mock(get_partial_messageable=Mock(return_value=self.channel))
        self.use_cases = PollUseCases(bot, self.repository, ballot_secret=SECRET, electorate_role_id=ROLE_ID)
        self.closed_at = datetime.datetime(2026, 9, 27, 12, 0, tzinfo=JST)

    def sent_title(self) -> str:
        """新しく投稿したEmbedのタイトルを返す。"""
        return self.channel.send.await_args.kwargs["embed"].title

    async def test_close_posts_the_result_as_a_reply(self) -> None:
        poll = make_record(closed_at=self.closed_at, result_counts=(1, 0), result_voter_count=1)
        self.repository.get.return_value = poll

        await self.use_cases._after_close(poll)  # noqa: SLF001

        ensure(self.sent_title().startswith("投票結果"))
        ensure(self.channel.send.await_args.kwargs["reference"].message_id == poll.message_id)
        self.repository.attach_result_message.assert_awaited_once_with(poll.id, 8)

    async def test_reopen_posts_a_notice_and_retires_the_previous_result(self) -> None:
        poll = make_record(result_counts=(1, 0), result_voter_count=1, result_message_id=PREVIOUS_RESULT_ID)
        self.repository.get.return_value = poll

        await self.use_cases._after_reopen(poll)  # noqa: SLF001

        ensure(self.sent_title().startswith("投票再開"))
        ensure(self.previous_result.edit.await_args.kwargs["view"] is None)
        self.repository.attach_result_message.assert_not_awaited()

    async def test_finalize_posts_the_final_result_as_a_new_message(self) -> None:
        poll = make_record(
            closed_at=self.closed_at,
            finalized_at=self.closed_at,
            result_counts=(1, 0),
            result_voter_count=1,
            result_message_id=PREVIOUS_RESULT_ID,
        )
        self.repository.get.return_value = poll

        await self.use_cases._after_finalize(poll)  # noqa: SLF001

        ensure(self.sent_title().startswith("確定結果"))
        ensure(self.previous_result.edit.await_args.kwargs == {"view": None})
        self.repository.attach_result_message.assert_awaited_once_with(poll.id, 8)
