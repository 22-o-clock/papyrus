"""秘密投票の状態を表す値。"""

import datetime
from dataclasses import dataclass
from enum import StrEnum

from .constants import REOPEN_WINDOW


class PollStatus(StrEnum):
    """投票の進行段階。"""

    OPEN = "open"
    # 締め切り後、作成者が再開できる期間。
    CLOSED = "closed"
    # 個々の票を削除し、結果が確定した状態。
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class PollRecord:
    """保存された投票1件。投票者に関する情報は含まない。

    Attributes:
        result_message_id: 直近の締め切りで投稿した結果メッセージのID。
        allow_change: 締め切りまで投票の変更・取り消しを認める変更可モードか。
        live_counts: 変更不可モードで投票ごとに加算している票数。締め切りまで公開しない。
        result_counts: 直近の締め切り時点の選択肢ごとの票数。再開後も次の締め切りまで保持する。
        result_voter_count: 直近の締め切り時点の投票者数。

    """

    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    result_message_id: int | None
    creator_id: int
    question: str
    options: tuple[str, ...]
    allow_multiple: bool
    allow_change: bool
    target_role_id: int
    closes_at: datetime.datetime
    closed_at: datetime.datetime | None
    finalized_at: datetime.datetime | None
    live_counts: tuple[int, ...] | None
    result_counts: tuple[int, ...] | None
    result_voter_count: int | None

    @property
    def status(self) -> PollStatus:
        """保存された時刻から、現在の進行段階を返す。"""
        if self.finalized_at is not None:
            return PollStatus.FINALIZED
        if self.closed_at is not None:
            return PollStatus.CLOSED
        return PollStatus.OPEN

    @property
    def finalizes_at(self) -> datetime.datetime | None:
        """締め切り済みの場合に、結果が確定する時刻を返す。"""
        return None if self.closed_at is None else self.closed_at + REOPEN_WINDOW

    def accepts_votes(self, now: datetime.datetime) -> bool:
        """指定時刻に投票を受け付けるかを返す。締め切り処理の前でも期限後は受け付けない。"""
        return self.status is PollStatus.OPEN and now < self.closes_at

    def can_reopen(self, now: datetime.datetime) -> bool:
        """指定時刻に再開できるかを返す。"""
        finalizes_at = self.finalizes_at
        return self.status is PollStatus.CLOSED and finalizes_at is not None and now < finalizes_at


class BallotOutcome(StrEnum):
    """投票・取り消しの操作結果。"""

    ACCEPTED = "accepted"
    NOT_OPEN = "not_open"
    NOT_ELIGIBLE = "not_eligible"
    # 変更不可モードで、既に投票している場合。
    ALREADY_VOTED = "already_voted"
    # 変更不可モードで、変更・取り消しを求められた場合。
    UNCHANGEABLE = "unchangeable"


@dataclass(frozen=True, slots=True)
class BallotResult:
    """投票・取り消しの操作結果と、操作後の投票者数。"""

    outcome: BallotOutcome
    voter_count: int = 0
