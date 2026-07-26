"""ランキング発表メッセージの配送記録。"""

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cogs.cynicism.database import CynicismDatabase, CynicismReportDelivery
from cogs.cynicism.periods import CynicismPeriod


@dataclass(frozen=True, slots=True)
class ReportDelivery:
    """特定の期間・投稿先に対応するDiscordメッセージ。"""

    period_type: str
    period_start: datetime.date
    target_id: int
    message_id: int
    content_digest: str
    first_posted_at: datetime.datetime
    last_updated_at: datetime.datetime


class CynicismReportRepository:
    """ランキング発表の配送履歴を管理する。"""

    def __init__(self, database: CynicismDatabase) -> None:
        self._database = database

    async def get_delivery(self, period: CynicismPeriod, target_id: int) -> ReportDelivery | None:
        """指定期間・投稿先に既存メッセージがあれば返す。"""
        async with self._database.session() as session:
            result = await session.execute(
                select(CynicismReportDelivery).where(
                    CynicismReportDelivery.period_type == period.period_type.value,
                    CynicismReportDelivery.period_start == period.start_date,
                    CynicismReportDelivery.target_id == target_id,
                )
            )
            row = result.scalar_one_or_none()
            return None if row is None else _to_delivery(row)

    async def has_delivery(self, period: CynicismPeriod, target_id: int) -> bool:
        """指定期間・投稿先の配送記録が存在するか返す。"""
        return await self.get_delivery(period, target_id) is not None

    async def save_delivery(
        self,
        period: CynicismPeriod,
        target_id: int,
        message_id: int,
        *,
        content_digest: str,
        posted_at: datetime.datetime,
    ) -> None:
        """新規投稿または更新したメッセージIDと内容の指紋を永続化する。"""
        async with self._database.session() as session:
            statement = (
                insert(CynicismReportDelivery)
                .values(
                    period_type=period.period_type.value,
                    period_start=period.start_date,
                    target_id=target_id,
                    message_id=message_id,
                    content_digest=content_digest,
                    first_posted_at=posted_at,
                    last_updated_at=posted_at,
                )
                .on_conflict_do_update(
                    index_elements=[
                        CynicismReportDelivery.period_type,
                        CynicismReportDelivery.period_start,
                        CynicismReportDelivery.target_id,
                    ],
                    set_={
                        "message_id": message_id,
                        "content_digest": content_digest,
                        "last_updated_at": posted_at,
                    },
                )
            )
            await session.execute(statement)
            await session.commit()

    async def get_last_delivery(self, target_id: int) -> ReportDelivery | None:
        """現在の投稿先で最後に成功した配送を返す。"""
        async with self._database.session() as session:
            result = await session.execute(
                select(CynicismReportDelivery)
                .where(CynicismReportDelivery.target_id == target_id)
                .order_by(CynicismReportDelivery.last_updated_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return None if row is None else _to_delivery(row)


def _to_delivery(row: CynicismReportDelivery) -> ReportDelivery:
    """ORM行をセッション外で安全に使える値へ変換する。"""
    return ReportDelivery(
        period_type=row.period_type,
        period_start=row.period_start,
        target_id=row.target_id,
        message_id=row.message_id,
        content_digest=row.content_digest,
        first_posted_at=row.first_posted_at,
        last_updated_at=row.last_updated_at,
    )
