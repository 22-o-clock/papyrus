"""投票、票、有権者リストの永続化。

投票の状態を変える操作は、投票の行をロックしてから行う。締め切り処理と投票の受け付けが
並行しても、締め切り後の票が集計から漏れたり、確定後に票が残ったりしないようにするため。
"""

import datetime
from collections.abc import Collection, Sequence

from sqlalchemy import RowMapping, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cogs.poll.constants import REOPEN_WINDOW
from cogs.poll.database import Poll, PollBallot, PollDatabase, PollElectorate
from cogs.poll.models import BallotOutcome, BallotResult, PollRecord

POLL_TABLE = Poll.__table__
AUTOCOMPLETE_LIMIT = 25


class PollRepository:
    """投票と票を実行環境別に保存する。"""

    def __init__(self, database: PollDatabase) -> None:
        """投票を保存する環境別DB接続を保持する。"""
        self._database = database

    async def create(  # noqa: PLR0913 - 作成時に指定する項目をそのまま受け取るため。
        self,
        *,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        question: str,
        options: Sequence[str],
        allow_multiple: bool,
        allow_change: bool,
        target_role_id: int,
        closes_at: datetime.datetime,
        electorate_ids: Collection[int],
    ) -> PollRecord:
        """告知メッセージを投稿する前の投票を、作成時点の有権者リストとともに作成する。"""
        async with self._database.session() as session:
            poll_id = await session.scalar(
                insert(Poll)
                .values(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    creator_id=creator_id,
                    question=question,
                    options=list(options),
                    allow_multiple=allow_multiple,
                    allow_change=allow_change,
                    target_role_id=target_role_id,
                    closes_at=closes_at,
                    live_counts=None if allow_change else [0] * len(options),
                )
                .returning(Poll.id)
            )
            if poll_id is None:
                message = "Failed to create a poll"
                raise RuntimeError(message)
            await session.execute(
                insert(PollElectorate), [{"poll_id": poll_id, "user_id": user_id} for user_id in electorate_ids]
            )
            poll = await _load(session, poll_id)
        if poll is None:
            message = "Failed to load the created poll"
            raise RuntimeError(message)
        return poll

    async def attach_message(self, poll_id: int, message_id: int) -> None:
        """投稿した告知メッセージを投票に関連付ける。"""
        async with self._database.session() as session:
            await session.execute(update(Poll).where(Poll.id == poll_id).values(message_id=message_id))

    async def attach_result_message(self, poll_id: int, message_id: int) -> None:
        """締め切り時に投稿した結果メッセージを投票に関連付ける。"""
        async with self._database.session() as session:
            await session.execute(update(Poll).where(Poll.id == poll_id).values(result_message_id=message_id))

    async def delete(self, poll_id: int) -> None:
        """告知メッセージを投稿できなかった投票を、関連する行ごと削除する。"""
        async with self._database.session() as session:
            await session.execute(delete(PollBallot).where(PollBallot.poll_id == poll_id))
            await session.execute(delete(PollElectorate).where(PollElectorate.poll_id == poll_id))
            await session.execute(delete(Poll).where(Poll.id == poll_id))

    async def get(self, poll_id: int) -> PollRecord | None:
        """投票を返す。存在しない場合はNoneを返す。"""
        async with self._database.session() as session:
            return await _load(session, poll_id)

    async def search(self, guild_id: int, query: str) -> list[tuple[int, str]]:
        """サーバー内の投票を新しい順に検索し、IDと質問の組を返す。"""
        statement = select(Poll.id, Poll.question).where(Poll.guild_id == guild_id, Poll.message_id.is_not(None))
        if query.strip():
            statement = statement.where(Poll.question.contains(query.strip(), autoescape=True))
        async with self._database.session() as session:
            rows = await session.execute(statement.order_by(Poll.id.desc()).limit(AUTOCOMPLETE_LIMIT))
            return [(poll_id, question) for poll_id, question in rows.all()]

    async def count_voters(self, poll_id: int) -> int:
        """現在の投票者数を返す。"""
        async with self._database.session() as session:
            return await _count_voters(session, poll_id)

    async def list_electorate(self, poll_id: int) -> list[int]:
        """作成時点の有権者のユーザーIDを返す。"""
        async with self._database.session() as session:
            rows = await session.execute(
                select(PollElectorate.user_id).where(PollElectorate.poll_id == poll_id).order_by(PollElectorate.user_id)
            )
            return [user_id for (user_id,) in rows.all()]

    async def count_electorate(self, poll_id: int) -> int:
        """作成時点の有権者数を返す。"""
        async with self._database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(PollElectorate).where(PollElectorate.poll_id == poll_id)
            )
            return count or 0

    async def is_eligible(self, poll_id: int, user_id: int) -> bool:
        """指定メンバーが作成時点の有権者かを返す。"""
        async with self._database.session() as session:
            return await _is_eligible(session, poll_id, user_id)

    async def has_voted(self, poll_id: int, voter_key: str) -> bool:
        """投票者が投票済みかを返す。"""
        async with self._database.session() as session:
            return await _has_ballot(session, poll_id, voter_key)

    async def get_choices(self, poll_id: int, voter_key: str) -> tuple[int, ...] | None:
        """変更可モードの投票者の現在の選択を返す。投票していない場合はNoneを返す。"""
        async with self._database.session() as session:
            choices = await session.scalar(
                select(PollBallot.choices).where(PollBallot.poll_id == poll_id, PollBallot.voter_key == voter_key)
            )
        return None if choices is None else tuple(choices)

    async def cast_ballot(
        self,
        poll_id: int,
        user_id: int,
        voter_key: str,
        choices: Sequence[int],
        now: datetime.datetime,
    ) -> BallotResult:
        """投票を保存する。変更可モードでは既存の票を上書きする。

        Args:
            poll_id: 投票のID。
            user_id: 有権者リストとの照合にだけ使うユーザーID。保存しない。
            voter_key: 票に保存する投票者の秘匿キー。
            choices: 選択肢の番号。
            now: 締め切りの判定に使う現在時刻。

        """
        async with self._database.session() as session:
            poll = await _load(session, poll_id, lock=True)
            if poll is None or not poll.accepts_votes(now):
                return BallotResult(BallotOutcome.NOT_OPEN)
            if not await _is_eligible(session, poll_id, user_id):
                return BallotResult(BallotOutcome.NOT_ELIGIBLE)
            if poll.allow_change:
                # 同じ投票の操作は行ロックで直列化されるため、削除してから挿入すれば上書きになる。
                await session.execute(
                    delete(PollBallot).where(PollBallot.poll_id == poll_id, PollBallot.voter_key == voter_key)
                )
                await session.execute(insert(PollBallot).values(poll_id=poll_id, voter_key=voter_key, choices=list(choices)))
            else:
                if await _has_ballot(session, poll_id, voter_key):
                    return BallotResult(BallotOutcome.ALREADY_VOTED, await _count_voters(session, poll_id))
                # 選択は票の行に残さず、投票の行の票数へ加算する。
                counts = list(poll.live_counts or [0] * len(poll.options))
                for index in choices:
                    counts[index] += 1
                await session.execute(insert(PollBallot).values(poll_id=poll_id, voter_key=voter_key, choices=None))
                await session.execute(update(Poll).where(Poll.id == poll_id).values(live_counts=counts))
            return BallotResult(BallotOutcome.ACCEPTED, await _count_voters(session, poll_id))

    async def withdraw_ballot(self, poll_id: int, voter_key: str, now: datetime.datetime) -> BallotResult:
        """変更可モードの票を取り消す。"""
        async with self._database.session() as session:
            poll = await _load(session, poll_id, lock=True)
            if poll is None or not poll.accepts_votes(now):
                return BallotResult(BallotOutcome.NOT_OPEN)
            if not poll.allow_change:
                return BallotResult(BallotOutcome.UNCHANGEABLE)
            await session.execute(delete(PollBallot).where(PollBallot.poll_id == poll_id, PollBallot.voter_key == voter_key))
            return BallotResult(BallotOutcome.ACCEPTED, await _count_voters(session, poll_id))

    async def close(self, poll_id: int, now: datetime.datetime) -> PollRecord | None:
        """受付中の投票を締め切り、集計結果を保存する。受付中でなければNoneを返す。

        再開に備えて、票の行は確定まで残す。
        """
        async with self._database.session() as session:
            poll = await _load(session, poll_id, lock=True)
            if poll is None or poll.closed_at is not None:
                return None
            if poll.live_counts is not None:
                counts = list(poll.live_counts)
            else:
                rows = await session.execute(select(PollBallot.choices).where(PollBallot.poll_id == poll_id))
                ballots = [tuple(choices or ()) for (choices,) in rows.all()]
                counts = [sum(index in choices for choices in ballots) for index in range(len(poll.options))]
            await session.execute(
                update(Poll)
                .where(Poll.id == poll_id)
                .values(closed_at=now, result_counts=counts, result_voter_count=await _count_voters(session, poll_id))
            )
            return await _load(session, poll_id)

    async def reopen(self, poll_id: int, now: datetime.datetime, closes_at: datetime.datetime) -> PollRecord | None:
        """再開可能期間内の投票を、票を引き継いで再開する。再開できなければNoneを返す。"""
        async with self._database.session() as session:
            poll = await _load(session, poll_id, lock=True)
            if poll is None or not poll.can_reopen(now):
                return None
            await session.execute(update(Poll).where(Poll.id == poll_id).values(closed_at=None, closes_at=closes_at))
            return await _load(session, poll_id)

    async def finalize(self, poll_id: int, now: datetime.datetime) -> PollRecord | None:
        """再開可能期間を過ぎた投票の票の行を削除し、結果を確定する。確定できなければNoneを返す。"""
        async with self._database.session() as session:
            poll = await _load(session, poll_id, lock=True)
            if poll is None or poll.closed_at is None or poll.finalized_at is not None or poll.can_reopen(now):
                return None
            await session.execute(delete(PollBallot).where(PollBallot.poll_id == poll_id))
            await session.execute(update(Poll).where(Poll.id == poll_id).values(finalized_at=now, live_counts=None))
            return await _load(session, poll_id)

    async def list_due_to_close(self, now: datetime.datetime) -> list[int]:
        """締め切り時刻を過ぎた受付中の投票IDを返す。"""
        async with self._database.session() as session:
            rows = await session.execute(
                select(Poll.id).where(Poll.closed_at.is_(None), Poll.closes_at <= now).order_by(Poll.closes_at, Poll.id)
            )
            return [poll_id for (poll_id,) in rows.all()]

    async def list_due_to_finalize(self, now: datetime.datetime) -> list[int]:
        """再開可能期間を過ぎた、未確定の投票IDを返す。"""
        async with self._database.session() as session:
            rows = await session.execute(
                select(Poll.id)
                .where(Poll.closed_at.is_not(None), Poll.finalized_at.is_(None), Poll.closed_at <= now - REOPEN_WINDOW)
                .order_by(Poll.closed_at, Poll.id)
            )
            return [poll_id for (poll_id,) in rows.all()]


async def _load(session: AsyncSession, poll_id: int, *, lock: bool = False) -> PollRecord | None:
    """投票を読み込む。lockを指定した場合は、トランザクション終了まで行をロックする。"""
    statement = select(POLL_TABLE).where(Poll.id == poll_id)
    if lock:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).mappings().first()
    return None if row is None else _to_record(row)


async def _count_voters(session: AsyncSession, poll_id: int) -> int:
    """投票者数を返す。"""
    count = await session.scalar(select(func.count()).select_from(PollBallot).where(PollBallot.poll_id == poll_id))
    return count or 0


async def _is_eligible(session: AsyncSession, poll_id: int, user_id: int) -> bool:
    """指定メンバーが作成時点の有権者かを返す。"""
    found = await session.scalar(
        select(PollElectorate.user_id).where(PollElectorate.poll_id == poll_id, PollElectorate.user_id == user_id)
    )
    return found is not None


async def _has_ballot(session: AsyncSession, poll_id: int, voter_key: str) -> bool:
    """投票者の票の行があるかを返す。"""
    found = await session.scalar(
        select(PollBallot.voter_key).where(PollBallot.poll_id == poll_id, PollBallot.voter_key == voter_key)
    )
    return found is not None


def _to_record(row: RowMapping) -> PollRecord:
    """DBの行を、投票者情報を含まない値へ変換する。"""
    return PollRecord(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        creator_id=row["creator_id"],
        question=row["question"],
        options=tuple(row["options"]),
        allow_multiple=row["allow_multiple"],
        allow_change=row["allow_change"],
        target_role_id=row["target_role_id"],
        closes_at=row["closes_at"],
        closed_at=row["closed_at"],
        finalized_at=row["finalized_at"],
        live_counts=_optional_tuple(row["live_counts"]),
        result_counts=_optional_tuple(row["result_counts"]),
        result_voter_count=row["result_voter_count"],
    )


def _optional_tuple(values: Sequence[int] | None) -> tuple[int, ...] | None:
    """NULL可のJSON配列をタプルへ変換する。"""
    return None if values is None else tuple(values)
