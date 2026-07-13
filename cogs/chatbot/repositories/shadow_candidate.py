import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import BigInteger, ForeignKey, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase


class ChatbotShadowCandidate(ChatbotBase):
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
    context_snapshot = mapped_column(JSONB, nullable=False)


class ChatbotShadowEvaluation(ChatbotBase):
    """管理者がCSVから取り込んだシャドー候補の評価。"""

    __tablename__ = "chatbot_shadow_evaluations"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chatbot.chatbot_shadow_candidates.id"),
        nullable=False,
    )
    evaluator_user_id = mapped_column(BigInteger, nullable=False)
    action_appropriate = mapped_column(Text, nullable=False)
    context_understood = mapped_column(Text, nullable=False)
    identity_correct = mapped_column(Text, nullable=False)
    length_natural = mapped_column(Text, nullable=False)
    non_intrusive = mapped_column(Text, nullable=False)
    worth_posting = mapped_column(Text, nullable=False)
    issue_category = mapped_column(Text, nullable=False)
    comment = mapped_column(Text, nullable=False)


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
    context_snapshot: list[dict[str, object]]


@dataclass
class ShadowEvaluationInput:
    """CSVから取り込む候補評価。"""

    candidate_id: uuid.UUID
    action_appropriate: str
    context_understood: str
    identity_correct: str
    length_natural: str
    non_intrusive: str
    worth_posting: str
    issue_category: str
    comment: str


class ChatbotShadowCandidateRepository:
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
                    context_snapshot=candidate.context_snapshot,
                )
            )

    async def get_unreviewed_candidates(
        self,
        evaluator_user_id: int,
        limit: int,
    ) -> list[ChatbotShadowCandidate]:
        """指定管理者が未評価の候補を古い順に取得します。"""
        evaluated_candidate_ids = select(ChatbotShadowEvaluation.candidate_id).where(
            ChatbotShadowEvaluation.evaluator_user_id == evaluator_user_id
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotShadowCandidate)
                .where(ChatbotShadowCandidate.id.not_in(evaluated_candidate_ids))
                .order_by(ChatbotShadowCandidate.created_at)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def save_evaluation(self, evaluator_user_id: int, evaluation: ShadowEvaluationInput) -> None:
        """管理者の評価を保存し、同じ候補への既存評価は上書きします。"""
        values = {
            "action_appropriate": evaluation.action_appropriate,
            "context_understood": evaluation.context_understood,
            "identity_correct": evaluation.identity_correct,
            "length_natural": evaluation.length_natural,
            "non_intrusive": evaluation.non_intrusive,
            "worth_posting": evaluation.worth_posting,
            "issue_category": evaluation.issue_category,
            "comment": evaluation.comment,
        }
        async with self._session_factory.begin() as session:
            existing_evaluation = await session.scalar(
                select(ChatbotShadowEvaluation).where(
                    ChatbotShadowEvaluation.candidate_id == evaluation.candidate_id,
                    ChatbotShadowEvaluation.evaluator_user_id == evaluator_user_id,
                )
            )
            if existing_evaluation is None:
                session.add(
                    ChatbotShadowEvaluation(
                        candidate_id=evaluation.candidate_id,
                        evaluator_user_id=evaluator_user_id,
                        **values,
                    )
                )
            else:
                for field, value in values.items():
                    setattr(existing_evaluation, field, value)
