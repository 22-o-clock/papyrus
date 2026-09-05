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
    """自己リアクションなどを除外した、発言者ごとの集計値。

    Attributes:
        member_id: リアクションを受けた発言者のID。
        human_count: 発言とリアクターの組を重複除去した件数。1件を1ポイントとする。
        cynical_message_count: 対象リアクションが1件以上付いた発言数。

    """

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
class MessageReactionCounts:
    """発言1件に付いたリアクションの集計値。"""

    message_id: int
    channel_id: int
    member_id: int
    reaction_count: int


@dataclass(frozen=True, slots=True)
class RankedMemberIdentity:
    """ランキング表示に必要なメンバー情報。"""

    member_id: int
    display_name: str
    is_bot: bool


@dataclass(frozen=True, slots=True)
class RankingEntry:
    """ランキング1行分の集計値。

    Attributes:
        rank: 同点を同順位とする順位。順位付け前は0。
        member_id: 発言者のID。
        display_name: Discordまたは保存済み情報から取得した表示名。
        points: 対象リアクションの合計件数。
        cynical_message_count: 対象リアクションが付いた発言数。
        message_count: 集計期間・チャンネル内の発言数。
        rate: 発言1件あたりのポイント。表示時に100倍して百分率とする。

    """

    rank: int
    member_id: int
    display_name: str
    points: int
    cynical_message_count: int
    message_count: int
    rate: float


@dataclass(frozen=True, slots=True)
class CynicismRanking:
    """冷笑率ランキングと参考の合計順位、最多ポイントの発言。

    合計と対象人数は参考順位から算出し、順位表との不整合を防ぐ。
    冷笑率の資格ラインはメンバーの順位にだけ適用し、最多ポイントの発言には適用しない。
    """

    period: CynicismPeriod
    total_entries: tuple[RankingEntry, ...]
    rate_entries: tuple[RankingEntry, ...]
    qualification_threshold: int
    top_messages: "tuple[TopCynicismMessage, ...]" = ()

    @property
    def total_points(self) -> int:
        """ランキング対象者の合計ポイントを返す。"""
        return sum(entry.points for entry in self.total_entries)

    @property
    def member_count(self) -> int:
        """ポイントを獲得したランキング対象者の人数を返す。"""
        return len(self.total_entries)

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
        """冷笑率による冷笑王を返す。同点なら表示順の先頭、資格到達者がいなければNone。"""
        return self.rate_entries[0] if self.rate_entries else None

    @property
    def qualified_member_count(self) -> int:
        """冷笑率部門の資格ラインに到達したメンバー数を返す。"""
        return len(self.rate_entries)


@dataclass(frozen=True, slots=True)
class TopCynicismMessage:
    """対象期間で最もポイントを集めた発言。"""

    message_id: int
    channel_id: int
    member_id: int
    display_name: str
    points: int
    guild_id: int

    @property
    def jump_url(self) -> str:
        """元の発言へのリンクを返す。"""
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"
