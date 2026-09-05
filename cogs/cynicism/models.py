"""冷笑ポイントの集計結果を表す値。"""

import datetime
from dataclasses import dataclass

from .periods import CynicismPeriod


@dataclass(frozen=True, slots=True)
class CynicismSettings:
    """一時停止状態を保持する運用設定。"""

    is_paused: bool
    paused_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class ChannelScope:
    """集計対象とするチャンネルの範囲。"""

    # Noneなら除外リスト以外の全チャンネルを対象とする。
    included_channel_ids: frozenset[int] | None
    excluded_channel_ids: frozenset[int]

    def contains(self, channel_id: int) -> bool:
        """指定チャンネルが集計対象かを返す。"""
        if self.included_channel_ids is not None:
            return channel_id in self.included_channel_ids
        return channel_id not in self.excluded_channel_ids


@dataclass(frozen=True, slots=True)
class MemberReactionCounts:
    """発言者ごとの🥶件数。"""

    member_id: int
    human_count: int
    cynical_message_count: int


@dataclass(frozen=True, slots=True)
class CynicismMessageRecord:
    """冷笑ポイントを獲得した発言1件と、🥶を向けたアカウントの内訳。"""

    message_id: int
    channel_id: int
    post_time: datetime.datetime
    content: str
    reactor_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RankedMemberIdentity:
    """ランキング表示に必要なメンバー情報。"""

    member_id: int
    display_name: str
    is_bot: bool


@dataclass(frozen=True, slots=True)
class RankingEntry:
    """ランキング1行分の集計値。"""

    rank: int
    member_id: int
    display_name: str
    points: int
    human_count: int
    cynical_message_count: int
    message_count: int
    rate: float


@dataclass(frozen=True, slots=True)
class CynicismRanking:
    """1つの期間に対する、合計部門と冷笑率部門の集計結果。"""

    period: CynicismPeriod
    total_entries: tuple[RankingEntry, ...]
    rate_entries: tuple[RankingEntry, ...]
    qualification_threshold: int
    total_points: int
    human_reaction_count: int
    member_count: int

    @property
    def is_empty(self) -> bool:
        """集計対象の🥶が1件も無いかを返す。"""
        return not self.total_entries

    @property
    def total_champion(self) -> RankingEntry | None:
        """合計部門の1位を返す。同点の場合は表示順で先頭の1名。"""
        return self.total_entries[0] if self.total_entries else None

    @property
    def rate_champion(self) -> RankingEntry | None:
        """冷笑率部門の1位を返す。資格ライン到達者がいなければNone。"""
        return self.rate_entries[0] if self.rate_entries else None

    @property
    def qualified_member_count(self) -> int:
        """冷笑率部門の資格ラインに到達したメンバー数を返す。"""
        return len(self.rate_entries)
