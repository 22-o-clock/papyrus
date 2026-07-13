import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.repositories.api_usage_report import ApiUsageReportConfiguration, ApiUsageReportDelivery

DEFAULT_REPORT_HOUR = 9
DEFAULT_REPORT_MINUTE = 0
CONFIGURATION_ID = 1


@dataclass(frozen=True, slots=True)
class ReportConfiguration:
    """日次レポートの投稿時刻。"""

    report_hour: int
    report_minute: int


@dataclass(frozen=True, slots=True)
class ReportDelivery:
    """指定日・指定投稿先に対応するDiscordメッセージ。"""

    report_date: datetime.date
    target_id: int
    message_id: int
    first_posted_at: datetime.datetime
    last_updated_at: datetime.datetime
    openai_cost_available: bool


class ApiUsageReportDatabase:
    """API利用量レポートの設定とDiscord配送履歴を管理する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_configuration(self) -> ReportConfiguration:
        """設定を取得し、未作成なら09:00 JSTで初期化する。"""
        async with self._session_factory.begin() as session:
            statement = (
                insert(ApiUsageReportConfiguration)
                .values(id=CONFIGURATION_ID, report_hour=DEFAULT_REPORT_HOUR, report_minute=DEFAULT_REPORT_MINUTE)
                .on_conflict_do_nothing(index_elements=[ApiUsageReportConfiguration.id])
            )
            await session.execute(statement)
            result = await session.execute(
                select(ApiUsageReportConfiguration).where(ApiUsageReportConfiguration.id == CONFIGURATION_ID)
            )
            row = result.scalar_one()
            return ReportConfiguration(row.report_hour, row.report_minute)

    async def set_report_time(self, hour: int, minute: int) -> None:
        """毎日の投稿時刻をJSTの時・分として保存する。"""
        async with self._session_factory.begin() as session:
            statement = (
                insert(ApiUsageReportConfiguration)
                .values(id=CONFIGURATION_ID, report_hour=hour, report_minute=minute)
                .on_conflict_do_update(
                    index_elements=[ApiUsageReportConfiguration.id],
                    set_={"report_hour": hour, "report_minute": minute},
                )
            )
            await session.execute(statement)

    async def get_delivery(self, report_date: datetime.date, target_id: int) -> ReportDelivery | None:
        """指定日・投稿先に既存メッセージがあれば返す。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ApiUsageReportDelivery).where(
                    ApiUsageReportDelivery.report_date == report_date,
                    ApiUsageReportDelivery.target_id == target_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._to_delivery(row)

    async def save_delivery(
        self,
        report_date: datetime.date,
        target_id: int,
        message_id: int,
        *,
        posted_at: datetime.datetime,
        openai_cost_available: bool,
    ) -> None:
        """新規投稿または更新したメッセージIDを永続化する。"""
        async with self._session_factory.begin() as session:
            statement = (
                insert(ApiUsageReportDelivery)
                .values(
                    report_date=report_date,
                    target_id=target_id,
                    message_id=message_id,
                    first_posted_at=posted_at,
                    last_updated_at=posted_at,
                    openai_cost_available=openai_cost_available,
                )
                .on_conflict_do_update(
                    index_elements=[ApiUsageReportDelivery.report_date, ApiUsageReportDelivery.target_id],
                    set_={
                        "message_id": message_id,
                        "last_updated_at": posted_at,
                        "openai_cost_available": openai_cost_available,
                    },
                )
            )
            await session.execute(statement)

    async def has_delivery(self, report_date: datetime.date, target_id: int) -> bool:
        """指定日・投稿先の配送記録が存在するか返す。"""
        return await self.get_delivery(report_date, target_id) is not None

    async def get_last_delivery(self, target_id: int) -> ReportDelivery | None:
        """現在の投稿先で最後に成功した配送を返す。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ApiUsageReportDelivery)
                .where(ApiUsageReportDelivery.target_id == target_id)
                .order_by(ApiUsageReportDelivery.last_updated_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return None if row is None else self._to_delivery(row)

    @staticmethod
    def _to_delivery(row: ApiUsageReportDelivery) -> ReportDelivery:
        """ORM行をセッション外で安全に使える値へ変換する。"""
        return ReportDelivery(
            report_date=row.report_date,
            target_id=row.target_id,
            message_id=row.message_id,
            first_posted_at=row.first_posted_at,
            last_updated_at=row.last_updated_at,
            openai_cost_available=row.openai_cost_available,
        )
