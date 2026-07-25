import datetime
from dataclasses import dataclass

from sqlalchemy import BigInteger, Date, DateTime, Integer, Text, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .base import ChatbotBase

UTC = datetime.UTC
MEASUREMENT_STATE_ID = 1


class ChatbotApiUsageDaily(ChatbotBase):
    """Chatbot機能別API利用量の日次集約。"""

    __tablename__ = "api_usage_daily"

    usage_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    long_context_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    long_context_cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    long_context_cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    long_context_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    web_search_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    code_interpreter_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChatbotApiUsageMeasurementState(ChatbotBase):
    """機能別計測を開始した日時を一度だけ保持する。"""

    __tablename__ = "api_usage_measurement_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True, slots=True)
class ApiUsageIncrement:
    """1回のAPI呼び出しから日次集約へ加算する値。"""

    operation: str
    model: str
    succeeded: bool
    item_count: int = 1
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    long_context_input_tokens: int = 0
    long_context_cached_input_tokens: int = 0
    long_context_cache_write_input_tokens: int = 0
    long_context_output_tokens: int = 0
    web_search_calls: int = 0
    code_interpreter_sessions: int = 0


class ChatbotApiUsageRepository:
    """ChatbotのAPI利用量を日次集約として永続化する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def initialize_measurement(self, *, started_at: datetime.datetime | None = None) -> None:
        """Bot起動時刻を機能別計測の開始時刻として未作成の場合だけ保存する。"""
        timestamp = started_at or datetime.datetime.now(UTC)
        async with self._session_factory.begin() as session:
            await self._ensure_measurement_state(session, timestamp)

    async def add(self, increment: ApiUsageIncrement, *, recorded_at: datetime.datetime | None = None) -> None:
        """呼び出し結果を該当するUTC日付の集約行へ原子的に加算する。"""
        timestamp = recorded_at or datetime.datetime.now(UTC)
        usage_date = utc_usage_date(timestamp)
        values = {
            "usage_date": usage_date,
            "operation": increment.operation,
            "model": increment.model,
            "success_count": int(increment.succeeded),
            "failure_count": int(not increment.succeeded),
            "item_count": increment.item_count,
            "input_tokens": increment.input_tokens,
            "cached_input_tokens": increment.cached_input_tokens,
            "cache_write_input_tokens": increment.cache_write_input_tokens,
            "output_tokens": increment.output_tokens,
            "long_context_input_tokens": increment.long_context_input_tokens,
            "long_context_cached_input_tokens": increment.long_context_cached_input_tokens,
            "long_context_cache_write_input_tokens": increment.long_context_cache_write_input_tokens,
            "long_context_output_tokens": increment.long_context_output_tokens,
            "web_search_calls": increment.web_search_calls,
            "code_interpreter_sessions": increment.code_interpreter_sessions,
        }
        async with self._session_factory.begin() as session:
            await self._ensure_measurement_state(session, timestamp)
            excluded = insert(ChatbotApiUsageDaily).excluded
            statement = (
                insert(ChatbotApiUsageDaily)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        ChatbotApiUsageDaily.usage_date,
                        ChatbotApiUsageDaily.operation,
                        ChatbotApiUsageDaily.model,
                    ],
                    set_={
                        "success_count": ChatbotApiUsageDaily.success_count + excluded.success_count,
                        "failure_count": ChatbotApiUsageDaily.failure_count + excluded.failure_count,
                        "item_count": ChatbotApiUsageDaily.item_count + excluded.item_count,
                        "input_tokens": ChatbotApiUsageDaily.input_tokens + excluded.input_tokens,
                        "cached_input_tokens": ChatbotApiUsageDaily.cached_input_tokens + excluded.cached_input_tokens,
                        "cache_write_input_tokens": (
                            ChatbotApiUsageDaily.cache_write_input_tokens + excluded.cache_write_input_tokens
                        ),
                        "output_tokens": ChatbotApiUsageDaily.output_tokens + excluded.output_tokens,
                        "long_context_input_tokens": (
                            ChatbotApiUsageDaily.long_context_input_tokens + excluded.long_context_input_tokens
                        ),
                        "long_context_cached_input_tokens": (
                            ChatbotApiUsageDaily.long_context_cached_input_tokens + excluded.long_context_cached_input_tokens
                        ),
                        "long_context_cache_write_input_tokens": (
                            ChatbotApiUsageDaily.long_context_cache_write_input_tokens
                            + excluded.long_context_cache_write_input_tokens
                        ),
                        "long_context_output_tokens": (
                            ChatbotApiUsageDaily.long_context_output_tokens + excluded.long_context_output_tokens
                        ),
                        "web_search_calls": ChatbotApiUsageDaily.web_search_calls + excluded.web_search_calls,
                        "code_interpreter_sessions": (
                            ChatbotApiUsageDaily.code_interpreter_sessions + excluded.code_interpreter_sessions
                        ),
                        "updated_at": func.now(),
                    },
                )
            )
            await session.execute(statement)

    async def list_for_date(self, usage_date: datetime.date) -> list[ChatbotApiUsageDaily]:
        """指定日の機能別集約を推定コスト計算用に返す。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotApiUsageDaily)
                .where(ChatbotApiUsageDaily.usage_date == usage_date)
                .order_by(ChatbotApiUsageDaily.operation, ChatbotApiUsageDaily.model)
            )
            return list(result.scalars())

    async def get_measurement_started_at(self) -> datetime.datetime | None:
        """機能別計測が最初に開始された日時を返す。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotApiUsageMeasurementState.started_at).where(
                    ChatbotApiUsageMeasurementState.id == MEASUREMENT_STATE_ID
                )
            )
            return result.scalar_one_or_none()

    async def _ensure_measurement_state(self, session: AsyncSession, started_at: datetime.datetime) -> None:
        """最初の計測時刻だけを競合に強い形で保存する。"""
        statement = (
            insert(ChatbotApiUsageMeasurementState)
            .values(id=MEASUREMENT_STATE_ID, started_at=started_at)
            .on_conflict_do_nothing(index_elements=[ChatbotApiUsageMeasurementState.id])
        )
        await session.execute(statement)


def utc_usage_date(recorded_at: datetime.datetime) -> datetime.date:
    """API呼び出し時刻をOpenAI Costsの日次境界と同じUTC日へ変換する。"""
    return recorded_at.astimezone(UTC).date()
