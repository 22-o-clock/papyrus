import datetime

from sqlalchemy import BigInteger, Text, delete, select, update
from sqlalchemy.dialects.postgresql import TIMESTAMP, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase


class ChatbotMemoryExtractionQueue(ChatbotBase):
    """まとめて抽出する長期記憶候補のメッセージキュー。"""

    __tablename__ = "chatbot_memory_extraction_queue"

    message_id = mapped_column(BigInteger, primary_key=True)
    channel_id = mapped_column(BigInteger, nullable=False, index=True)
    queued_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    attempt_count = mapped_column(BigInteger, nullable=False, default=0)


class ChatbotMemoryExtractionQueueRepository:
    """長期記憶抽出の対象メッセージを永続化します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(self, message_id: int, channel_id: int) -> None:
        """人間投稿を未処理キューへ追加し、編集後の投稿は再処理対象へ戻します。"""
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            statement = insert(ChatbotMemoryExtractionQueue).values(
                message_id=message_id, channel_id=channel_id, queued_at=now, status="pending", attempt_count=0
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatbotMemoryExtractionQueue.message_id],
                    set_={"channel_id": channel_id, "queued_at": now, "status": "pending", "attempt_count": 0},
                )
            )

    async def delete(self, message_id: int) -> None:
        """削除された投稿を抽出待ちキューから取り除きます。"""
        async with self._session_factory.begin() as session:
            await session.execute(
                delete(ChatbotMemoryExtractionQueue).where(ChatbotMemoryExtractionQueue.message_id == message_id)
            )

    async def count_pending(self) -> int:
        """抽出待ち投稿の件数を返します。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotMemoryExtractionQueue.message_id).where(ChatbotMemoryExtractionQueue.status == "pending")
            )
            return len(result.scalars().all())

    async def claim_pending(self, limit: int) -> list[ChatbotMemoryExtractionQueue]:
        """未処理キューを古い順に取得し、処理中へ遷移させます。"""
        async with self._session_factory.begin() as session:
            result = await session.execute(
                select(ChatbotMemoryExtractionQueue)
                .where(ChatbotMemoryExtractionQueue.status == "pending")
                .order_by(ChatbotMemoryExtractionQueue.queued_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            items = list(result.scalars().all())
            for item in items:
                item.status = "processing"
                item.attempt_count += 1
            return items

    async def complete(self, message_ids: list[int]) -> None:
        """抽出済み投稿をキューから除去します。"""
        if not message_ids:
            return
        async with self._session_factory.begin() as session:
            await session.execute(
                delete(ChatbotMemoryExtractionQueue).where(ChatbotMemoryExtractionQueue.message_id.in_(message_ids))
            )

    async def restore_pending(self, message_ids: list[int]) -> None:
        """失敗した抽出対象を次回の再試行へ戻します。"""
        if not message_ids:
            return
        async with self._session_factory.begin() as session:
            await session.execute(
                update(ChatbotMemoryExtractionQueue)
                .where(ChatbotMemoryExtractionQueue.message_id.in_(message_ids))
                .values(status="pending")
            )

    async def recover_interrupted(self) -> None:
        """前回終了時に処理中だった投稿を再試行対象へ戻します。"""
        async with self._session_factory.begin() as session:
            await session.execute(
                update(ChatbotMemoryExtractionQueue)
                .where(ChatbotMemoryExtractionQueue.status == "processing")
                .values(status="pending")
            )
