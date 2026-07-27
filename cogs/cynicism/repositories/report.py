"""ランキング発表の処理記録。"""

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cogs.cynicism.constants import EMPTY_STATUS, POSTED_STATUS
from cogs.cynicism.database import CynicismDatabase, CynicismReportDelivery
from cogs.cynicism.periods import CynicismPeriod


@dataclass(frozen=True, slots=True)
class ReportDelivery:
    """特定の期間・投稿先に対する処理結果。"""

    period_type: str
    period_start: datetime.date
    target_id: int
    status: str
    message_id: int | None
    content_digest: str
    first_processed_at: datetime.datetime
    last_processed_at: datetime.datetime

    @property
    def is_posted(self) -> bool:
        """Discordへ投稿済みかを返す。"""
        return self.status == POSTED_STATUS


class CynicismReportRepository:
    """ランキング発表の処理履歴を管理する。"""

    def __init__(self, database: CynicismDatabase) -> None:
        self._database = database

    async def get_delivery(self, period: CynicismPeriod, target_id: int) -> ReportDelivery | None:
        """指定期間・投稿先の処理記録があれば返す。"""
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
        """指定期間・投稿先が処理済みかを返す。投稿しなかった期間も処理済みとして扱う。"""
        return await self.get_delivery(period, target_id) is not None

    async def save_posted(
        self,
        period: CynicismPeriod,
        target_id: int,
        message_id: int,
        *,
        content_digest: str,
        processed_at: datetime.datetime,
    ) -> None:
        """投稿または更新したメッセージIDと内容の指紋を永続化する。"""
        await self._save(
            period,
            target_id,
            status=POSTED_STATUS,
            message_id=message_id,
            content_digest=content_digest,
            processed_at=processed_at,
        )

    async def save_empty(
        self,
        period: CynicismPeriod,
        target_id: int,
        *,
        processed_at: datetime.datetime,
    ) -> None:
        """対象の🥶が無く投稿しなかったことを記録し、毎分の再集計を防ぐ。"""
        await self._save(
            period,
            target_id,
            status=EMPTY_STATUS,
            message_id=None,
            content_digest="",
            processed_at=processed_at,
        )

    async def get_last_delivery(self, target_id: int) -> ReportDelivery | None:
        """現在の投稿先で最後に投稿した記録を返す。"""
        async with self._database.session() as session:
            result = await session.execute(
                select(CynicismReportDelivery)
                .where(
                    CynicismReportDelivery.target_id == target_id,
                    CynicismReportDelivery.status == POSTED_STATUS,
                )
                .order_by(CynicismReportDelivery.last_processed_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return None if row is None else _to_delivery(row)

    async def _save(  # noqa: PLR0913 - 冪等な保存に必要な項目をそのまま受け取る。
        self,
        period: CynicismPeriod,
        target_id: int,
        *,
        status: str,
        message_id: int | None,
        content_digest: str,
        processed_at: datetime.datetime,
    ) -> None:
        """処理結果を冪等に保存する。"""
        async with self._database.session() as session:
            statement = (
                insert(CynicismReportDelivery)
                .values(
                    period_type=period.period_type.value,
                    period_start=period.start_date,
                    target_id=target_id,
                    status=status,
                    message_id=message_id,
                    content_digest=content_digest,
                    first_processed_at=processed_at,
                    last_processed_at=processed_at,
                )
                .on_conflict_do_update(
                    index_elements=[
                        CynicismReportDelivery.period_type,
                        CynicismReportDelivery.period_start,
                        CynicismReportDelivery.target_id,
                    ],
                    set_={
                        "status": status,
                        "message_id": message_id,
                        "content_digest": content_digest,
                        "last_processed_at": processed_at,
                    },
                )
            )
            await session.execute(statement)


def _to_delivery(row: CynicismReportDelivery) -> ReportDelivery:
    """ORM行をセッション外で安全に使える値へ変換する。"""
    return ReportDelivery(
        period_type=row.period_type,
        period_start=row.period_start,
        target_id=row.target_id,
        status=row.status,
        message_id=row.message_id,
        content_digest=row.content_digest,
        first_processed_at=row.first_processed_at,
        last_processed_at=row.last_processed_at,
    )
