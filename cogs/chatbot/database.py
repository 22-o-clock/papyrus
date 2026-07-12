import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import BigInteger, Text, delete
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from core.db import Base


class ChatbotShadowCandidate(Base):
    """シャドーモードで保存する自発反応候補。"""

    __tablename__ = "chatbot_shadow_candidates"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id = mapped_column(BigInteger, nullable=False)
    trigger_message_id = mapped_column(BigInteger, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    action = mapped_column(Text, nullable=False)
    reply_to_message_id = mapped_column(BigInteger, nullable=True)
    content = mapped_column(Text, nullable=False)
    reaction_emoji = mapped_column(Text, nullable=True)
    reason = mapped_column(Text, nullable=False)
    context_message_ids = mapped_column(JSONB, nullable=False)


@dataclass
class ShadowCandidateInput:
    """保存するシャドー候補の内容。"""

    channel_id: int
    trigger_message_id: int
    action: str
    reply_to_message_id: int | None
    content: str
    reaction_emoji: str | None
    reason: str
    context_message_ids: list[int]


class ChatbotShadowCandidateStore:
    """シャドー候補の保存と期限切れデータの削除を行います。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, candidate: ShadowCandidateInput) -> None:
        """候補を保存し、90日を過ぎた候補を削除します。"""
        now = datetime.datetime.now(datetime.UTC)
        expiration = now - datetime.timedelta(days=90)
        async with self._session_factory.begin() as session:
            await session.execute(delete(ChatbotShadowCandidate).where(ChatbotShadowCandidate.created_at < expiration))
            session.add(
                ChatbotShadowCandidate(
                    channel_id=candidate.channel_id,
                    trigger_message_id=candidate.trigger_message_id,
                    created_at=now,
                    action=candidate.action,
                    reply_to_message_id=candidate.reply_to_message_id,
                    content=candidate.content,
                    reaction_emoji=candidate.reaction_emoji,
                    reason=candidate.reason,
                    context_message_ids=candidate.context_message_ids,
                )
            )
