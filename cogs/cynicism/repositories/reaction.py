"""🥶の記録と、発言者ごとの集計。"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, distinct, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from cogs.cynicism.constants import JST, REACTION_SOURCE
from cogs.cynicism.database import CynicismDatabase, CynicismReaction
from cogs.cynicism.models import ChannelScope, MemberReactionCounts
from cogs.cynicism.periods import CynicismPeriod
from cogs.talkdata.database import DiscordMember, DiscordMessage

# TalkDataが外部キー参照のために持つダミーレコード。集計対象から除外する。
TALKDATA_DUMMY_ID = 0
# 編集履歴の行を除き、1メッセージにつき1行だけを対象にする。
ORIGINAL_EDIT_COUNT = 0


@dataclass(frozen=True, slots=True)
class CynicismReactionEvent:
    """記録する🥶1件の内容。"""

    message_id: int
    reactor_id: int
    is_burst: bool
    source: str
    evidence_message_id: int | None


class CynicismReactionRepository:
    """🥶の付与記録を保存し、期間ごとの冷笑ポイントを集計する。"""

    def __init__(self, database: CynicismDatabase) -> None:
        self._database = database

    async def record(self, event: CynicismReactionEvent) -> None:
        """🥶を1件記録する。同じ根拠が既にあれば何もしない。"""
        async with self._database.session() as session:
            statement = (
                insert(CynicismReaction)
                .values(
                    message_id=event.message_id,
                    reactor_id=event.reactor_id,
                    is_burst=event.is_burst,
                    source=event.source,
                    evidence_message_id=event.evidence_message_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        CynicismReaction.message_id,
                        CynicismReaction.reactor_id,
                        CynicismReaction.is_burst,
                        CynicismReaction.source,
                    ]
                )
            )
            await session.execute(statement)

    async def remove_reaction(self, message_id: int, reactor_id: int, *, is_burst: bool) -> None:
        """取り消されたリアクション1件分の記録を削除する。"""
        await self._delete(
            CynicismReaction.message_id == message_id,
            CynicismReaction.reactor_id == reactor_id,
            CynicismReaction.is_burst == is_burst,
            CynicismReaction.source == REACTION_SOURCE,
        )

    async def remove_message_reactions(self, message_id: int) -> None:
        """メッセージからリアクションが一括削除された場合の記録を削除する。"""
        await self._delete(
            CynicismReaction.message_id == message_id,
            CynicismReaction.source == REACTION_SOURCE,
        )

    async def remove_reply_evidence(self, evidence_message_id: int) -> None:
        """根拠となった🥶だけの返信が削除された場合の記録を削除する。"""
        await self._delete(CynicismReaction.evidence_message_id == evidence_message_id)

    async def earliest_recorded_date(self) -> datetime.date | None:
        """記録済みの🥶が向けられた、最も古い発言の日付(JST)を返す。"""
        statement = select(func.min(DiscordMessage.post_time)).select_from(CynicismReaction).join(*_talkdata_join())
        async with self._database.session() as session:
            earliest = await session.scalar(statement)
            return None if earliest is None else earliest.astimezone(JST).date()

    async def aggregate_counts(
        self,
        period: CynicismPeriod,
        *,
        papyrus_user_id: int,
        scope: ChannelScope,
    ) -> list[MemberReactionCounts]:
        """期間内の発言に向けられた🥶を、発言者ごとの件数へ集計する。"""
        # 同一リアクターがリアクションと返信の両方で🥶を向けても1件として数える。
        reactor_pairs = distinct(tuple_(CynicismReaction.message_id, CynicismReaction.reactor_id))
        statement = (
            select(
                DiscordMessage.member_id,
                func.count(reactor_pairs).filter(CynicismReaction.reactor_id == papyrus_user_id),
                func.count(reactor_pairs).filter(CynicismReaction.reactor_id != papyrus_user_id),
                func.count(distinct(CynicismReaction.message_id)),
            )
            .select_from(CynicismReaction)
            .join(*_talkdata_join())
            .where(
                DiscordMessage.post_time >= period.start_at,
                DiscordMessage.post_time < period.end_at,
                # 自分の発言へ自分で🥶を付けてポイントを稼げないようにする。
                CynicismReaction.reactor_id != DiscordMessage.member_id,
                DiscordMessage.member_id != TALKDATA_DUMMY_ID,
                *_scope_conditions(DiscordMessage.channel_id, scope),
            )
            .group_by(DiscordMessage.member_id)
        )

        async with self._database.session() as session:
            result = await session.execute(statement)
            return [
                MemberReactionCounts(
                    member_id=member_id,
                    papyrus_count=papyrus_count,
                    human_count=human_count,
                    cynical_message_count=cynical_message_count,
                )
                for member_id, papyrus_count, human_count, cynical_message_count in result.all()
            ]

    async def aggregate_message_counts(
        self,
        period: CynicismPeriod,
        *,
        scope: ChannelScope,
    ) -> dict[int, int]:
        """期間内の発言数をメンバーごとに返す。編集履歴の行は数えない。"""
        statement = (
            select(DiscordMessage.member_id, func.count(distinct(DiscordMessage.id)).label("message_count"))
            .where(
                DiscordMessage.edit_count == ORIGINAL_EDIT_COUNT,
                DiscordMessage.post_time >= period.start_at,
                DiscordMessage.post_time < period.end_at,
                DiscordMessage.id != TALKDATA_DUMMY_ID,
                DiscordMessage.member_id != TALKDATA_DUMMY_ID,
                *_scope_conditions(DiscordMessage.channel_id, scope),
            )
            .group_by(DiscordMessage.member_id)
        )

        async with self._database.session() as session:
            result = await session.execute(statement)
            return {row.member_id: row.message_count for row in result.all()}

    async def get_display_names(self, member_ids: Sequence[int]) -> dict[int, str]:
        """TalkDataに記録された表示名を返す。退出済みメンバーの表示に使う。"""
        if not member_ids:
            return {}
        async with self._database.session() as session:
            result = await session.execute(
                select(DiscordMember.id, DiscordMember.display_name).where(DiscordMember.id.in_(member_ids))
            )
            return {row.id: row.display_name for row in result.all()}

    async def _delete(self, *conditions: ColumnElement[bool]) -> None:
        """条件に一致する記録を削除する。"""
        async with self._database.session() as session:
            await session.execute(delete(CynicismReaction).where(*conditions))


def _talkdata_join() -> tuple[type[DiscordMessage], ColumnElement[bool]]:
    """🥶の記録を、TalkDataが持つ元の投稿へ結合する条件を返す。"""
    return (
        DiscordMessage,
        (DiscordMessage.id == CynicismReaction.message_id) & (DiscordMessage.edit_count == ORIGINAL_EDIT_COUNT),
    )


def _scope_conditions(
    channel_id_column: InstrumentedAttribute[int],
    scope: ChannelScope,
) -> list[ColumnElement[bool]]:
    """集計対象のチャンネル範囲を絞り込み条件へ変換する。"""
    if scope.included_channel_ids is not None:
        return [channel_id_column.in_(scope.included_channel_ids)]
    if scope.excluded_channel_ids:
        return [channel_id_column.notin_(scope.excluded_channel_ids)]
    return []
