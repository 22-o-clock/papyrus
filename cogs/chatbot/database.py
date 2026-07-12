import datetime
import uuid
from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, ForeignKey, MetaData, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.sql import text

from core.db import create_tables_for

CHATBOT_DATABASE_SCHEMA = "chatbot"


class ChatbotBase(DeclarativeBase):
    """chatbot専用DBのテーブル定義の基底クラス。"""

    metadata = MetaData(schema=CHATBOT_DATABASE_SCHEMA)


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


class ChatbotStoredMessage(ChatbotBase):
    """期限付きで保存する短期文脈メッセージ。"""

    __tablename__ = "chatbot_stored_messages"

    message_id = mapped_column(BigInteger, primary_key=True)
    channel_id = mapped_column(BigInteger, nullable=False, index=True)
    author_id = mapped_column(BigInteger, nullable=False)
    author_name = mapped_column(Text, nullable=False)
    content = mapped_column(Text, nullable=False)
    reply_to_message_id = mapped_column(BigInteger, nullable=True)
    mentioned_user_ids = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)
    is_bot = mapped_column(Boolean, nullable=False)


class ChatbotStoredAttachment(ChatbotBase):
    """短期文脈メッセージに付随する添付と解析結果。"""

    __tablename__ = "chatbot_stored_attachments"

    id = mapped_column(BigInteger, primary_key=True)
    message_id = mapped_column(
        BigInteger,
        ForeignKey("chatbot.chatbot_stored_messages.message_id"),
        nullable=False,
        index=True,
    )
    url = mapped_column(Text, nullable=False)
    filename = mapped_column(Text, nullable=False)
    content_type = mapped_column(Text, nullable=True)
    kind = mapped_column(Text, nullable=False)
    summary = mapped_column(Text, nullable=True)
    important_text = mapped_column(Text, nullable=True)
    analysis_status = mapped_column(Text, nullable=False)
    analyzed_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ChatbotLongTermMemory(ChatbotBase):
    """根拠と状態を保持する長期記憶。"""

    __tablename__ = "chatbot_long_term_memories"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_user_id = mapped_column(BigInteger, nullable=True, index=True)
    kind = mapped_column(Text, nullable=False)
    content = mapped_column(Text, nullable=False)
    source_type = mapped_column(Text, nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    is_sensitive = mapped_column(Boolean, nullable=False, default=False)
    is_pinned = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)
    last_referenced_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    invalidated_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    embedding = mapped_column(Vector(1536), nullable=True)


class ChatbotLongTermMemoryEvidence(ChatbotBase):
    """長期記憶を裏付けるDiscord投稿。"""

    __tablename__ = "chatbot_long_term_memory_evidences"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chatbot.chatbot_long_term_memories.id"),
        nullable=False,
        index=True,
    )
    message_id = mapped_column(BigInteger, nullable=False, index=True)
    author_id = mapped_column(BigInteger, nullable=False)
    excerpt = mapped_column(Text, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ChatbotMemoryExtractionQueue(ChatbotBase):
    """まとめて抽出する長期記憶候補のメッセージキュー。"""

    __tablename__ = "chatbot_memory_extraction_queue"

    message_id = mapped_column(BigInteger, primary_key=True)
    channel_id = mapped_column(BigInteger, nullable=False, index=True)
    queued_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    attempt_count = mapped_column(BigInteger, nullable=False, default=0)


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
class StoredMessageInput:
    """短期文脈として保存するDiscordメッセージ。"""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    reply_to_message_id: int | None
    mentioned_user_ids: list[int]
    created_at: datetime.datetime
    is_bot: bool


@dataclass
class StoredAttachmentInput:
    """短期文脈として保存する添付ファイル。"""

    id: int
    message_id: int
    url: str
    filename: str
    content_type: str | None
    kind: str


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


class ChatbotShortTermMessageStore:
    """短期文脈メッセージを30日間保存します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, message: StoredMessageInput) -> None:
        """メッセージを保存し、期限切れの本文を削除します。"""
        expiration = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
        async with self._session_factory.begin() as session:
            expired_message_ids = select(ChatbotStoredMessage.message_id).where(ChatbotStoredMessage.created_at < expiration)
            await session.execute(
                delete(ChatbotStoredAttachment).where(ChatbotStoredAttachment.message_id.in_(expired_message_ids))
            )
            await session.execute(delete(ChatbotStoredMessage).where(ChatbotStoredMessage.created_at < expiration))
            existing = await session.get(ChatbotStoredMessage, message.message_id)
            if existing is None:
                session.add(
                    ChatbotStoredMessage(
                        message_id=message.message_id,
                        channel_id=message.channel_id,
                        author_id=message.author_id,
                        author_name=message.author_name,
                        content=message.content,
                        reply_to_message_id=message.reply_to_message_id,
                        mentioned_user_ids=message.mentioned_user_ids,
                        created_at=message.created_at,
                        is_bot=message.is_bot,
                    )
                )
                return
            existing.author_name = message.author_name
            existing.content = message.content
            existing.reply_to_message_id = message.reply_to_message_id
            existing.mentioned_user_ids = message.mentioned_user_ids
            existing.is_bot = message.is_bot

    async def delete(self, message_id: int) -> None:
        """削除されたDiscordメッセージを短期保存から除去します。"""
        async with self._session_factory.begin() as session:
            # 添付の外部キーにDB側のCASCADE指定はしていないため、投稿削除時に明示的に消す。
            await session.execute(delete(ChatbotStoredAttachment).where(ChatbotStoredAttachment.message_id == message_id))
            await session.execute(delete(ChatbotStoredMessage).where(ChatbotStoredMessage.message_id == message_id))

    async def delete_attachments(self, message_id: int) -> None:
        """編集前の添付解析結果を除去します。"""
        async with self._session_factory.begin() as session:
            await session.execute(delete(ChatbotStoredAttachment).where(ChatbotStoredAttachment.message_id == message_id))

    async def save_attachment(self, attachment: StoredAttachmentInput) -> None:
        """添付メタデータを保存し、未解析状態へ初期化します。"""
        async with self._session_factory.begin() as session:
            existing = await session.get(ChatbotStoredAttachment, attachment.id)
            if existing is None:
                session.add(
                    ChatbotStoredAttachment(
                        id=attachment.id,
                        message_id=attachment.message_id,
                        url=attachment.url,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        kind=attachment.kind,
                        analysis_status="pending",
                    )
                )
                return
            existing.url = attachment.url
            existing.filename = attachment.filename
            existing.content_type = attachment.content_type
            existing.kind = attachment.kind
            existing.summary = None
            existing.important_text = None
            existing.analysis_status = "pending"
            existing.analyzed_at = None

    async def get_for_channel(self, channel_id: int) -> list[ChatbotStoredMessage]:
        """指定チャンネルの保存済み短期文脈を時系列順で取得します。"""
        expiration = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotStoredMessage)
                .where(ChatbotStoredMessage.channel_id == channel_id, ChatbotStoredMessage.created_at >= expiration)
                .order_by(ChatbotStoredMessage.created_at)
            )
            return list(result.scalars().all())

    async def get_attachments(self, message_ids: list[int]) -> list[ChatbotStoredAttachment]:
        """指定メッセージに紐づく保存済み添付を取得します。"""
        if not message_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotStoredAttachment)
                .where(ChatbotStoredAttachment.message_id.in_(message_ids))
                .order_by(ChatbotStoredAttachment.id)
            )
            return list(result.scalars().all())

    async def save_attachment_analysis(
        self,
        attachment_id: int,
        *,
        summary: str | None,
        important_text: str | None,
        status: str,
    ) -> None:
        """添付解析の結果または失敗状態を保存します。"""
        async with self._session_factory.begin() as session:
            attachment = await session.get(ChatbotStoredAttachment, attachment_id)
            if attachment is None:
                return
            attachment.summary = summary
            attachment.important_text = important_text
            attachment.analysis_status = status
            attachment.analyzed_at = datetime.datetime.now(datetime.UTC)


async def create_chatbot_tables(engine: AsyncEngine) -> None:
    """chatbotスキーマのテーブルと意味検索用拡張を作成します。"""
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
    await create_tables_for(engine, ChatbotBase.metadata, schema=CHATBOT_DATABASE_SCHEMA)
