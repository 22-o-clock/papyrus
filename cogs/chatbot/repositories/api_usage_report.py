import datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from .base import ChatbotBase


class ApiUsageReportConfiguration(ChatbotBase):
    """API利用量レポートの永続設定。"""

    __tablename__ = "api_usage_report_configuration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    report_minute: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class ApiUsageReportDelivery(ChatbotBase):
    """同じ対象日のDiscord投稿を再起動後も更新するための配送記録。"""

    __tablename__ = "api_usage_report_deliveries"

    report_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    target_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_posted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    openai_cost_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
