import datetime
import uuid
from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, ForeignKey, MetaData, Text, UniqueConstraint, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.sql import text

from core.db import create_tables_for

CHATBOT_DATABASE_SCHEMA = "chatbot"
EMBEDDING_DIMENSIONS = 3072


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
    external_entity_name = mapped_column(Text, nullable=True, index=True)
    target_resolution = mapped_column(Text, nullable=False, default="unresolved")
    kind = mapped_column(Text, nullable=False)
    content = mapped_column(Text, nullable=False)
    source_type = mapped_column(Text, nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    is_sensitive = mapped_column(Boolean, nullable=False, default=False)
    is_pinned = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)
    observed_at = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    last_referenced_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    invalidated_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    superseded_by_memory_id = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    conflict_group_id = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    embedding = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)


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


class ChatbotLongTermMemoryChange(ChatbotBase):
    """長期記憶の訂正・否定・競合の監査記録。"""

    __tablename__ = "chatbot_long_term_memory_changes"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action = mapped_column(Text, nullable=False)
    new_memory_id = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    existing_memory_ids = mapped_column(JSONB, nullable=False)
    evidence_message_ids = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)


class ChatbotLongTermMemoryAdminHistory(ChatbotBase):
    """管理者による長期記憶の一括変更履歴。"""

    __tablename__ = "chatbot_long_term_memory_admin_histories"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    administrator_user_id = mapped_column(BigInteger, nullable=False, index=True)
    before_value = mapped_column(JSONB, nullable=False)
    after_value = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)


class ChatbotMemberAlias(ChatbotBase):
    """サーバーメンバーを会話中の呼称へ結び付けます。"""

    __tablename__ = "chatbot_member_aliases"
    __table_args__ = (UniqueConstraint("normalized_alias", "target_user_id"),)

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias = mapped_column(Text, nullable=False)
    normalized_alias = mapped_column(Text, nullable=False, index=True)
    target_user_id = mapped_column(BigInteger, nullable=False, index=True)
    status = mapped_column(Text, nullable=False, index=True)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    invalidated_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ChatbotMemberAliasEvidence(ChatbotBase):
    """メンバー別名の判断根拠となったDiscord投稿。"""

    __tablename__ = "chatbot_member_alias_evidences"
    __table_args__ = (UniqueConstraint("alias_id", "message_id"),)

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chatbot.chatbot_member_aliases.id"),
        nullable=False,
        index=True,
    )
    message_id = mapped_column(BigInteger, nullable=False, index=True)
    author_id = mapped_column(BigInteger, nullable=False)
    excerpt = mapped_column(Text, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ChatbotMemberAliasHistory(ChatbotBase):
    """管理者による別名の一括変更履歴。"""

    __tablename__ = "chatbot_member_alias_histories"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias_id = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    administrator_user_id = mapped_column(BigInteger, nullable=False, index=True)
    before_value = mapped_column(JSONB, nullable=False)
    after_value = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)


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
class LongTermMemoryInput:
    """保存する根拠付き長期記憶。"""

    target_user_id: int | None
    external_entity_name: str | None
    target_resolution: str
    kind: str
    content: str
    source_type: str
    is_sensitive: bool
    evidence_message_ids: list[int]
    evidence_author_ids: list[int]
    evidence_excerpts: list[str]
    embedding: list[float]
    observed_at: datetime.datetime


@dataclass
class MemoryReconciliationInput:
    """既存記憶へ適用する訂正・否定・競合関係。"""

    action: str
    existing_memory_ids: list[uuid.UUID]
    evidence_message_ids: list[int]


@dataclass
class LongTermMemoryEvidenceRecord:
    """管理Excelへ表示する長期記憶の根拠投稿。"""

    message_id: int
    author_name: str
    excerpt: str
    channel_id: int | None


@dataclass
class LongTermMemoryReviewRecord:
    """管理Excelへ出力する長期記憶の現在値。"""

    memory: ChatbotLongTermMemory
    evidences: list[LongTermMemoryEvidenceRecord]


@dataclass
class LongTermMemoryUpdateInput:
    """管理Excelから一括適用する長期記憶の変更。"""

    memory_id: uuid.UUID
    action: str
    content: str
    target_user_id: int | None
    external_entity_name: str | None
    target_resolution: str
    kind: str
    source_type: str
    is_sensitive: bool
    expires_at: datetime.datetime | None
    embedding: list[float] | None


@dataclass
class MemberAliasInput:
    """保存するメンバー別名と根拠投稿。"""

    alias: str
    target_user_id: int
    evidence_message_ids: list[int]
    evidence_author_ids: list[int]
    evidence_excerpts: list[str]


@dataclass
class MemberAliasEvidenceRecord:
    """Excelへ表示する別名の根拠投稿。"""

    message_id: int
    author_name: str
    excerpt: str
    channel_id: int | None


@dataclass
class MemberAliasReviewRecord:
    """Excelで確認する別名と根拠の現在値。"""

    id: uuid.UUID
    alias: str
    normalized_alias: str
    target_user_id: int
    status: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    evidences: list[MemberAliasEvidenceRecord]


@dataclass
class MemberAliasUpdateInput:
    """Excelから一括適用する別名の変更。"""

    alias_id: uuid.UUID
    alias: str
    action: str
    target_user_id: int | None


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
            values = {
                "message_id": message.message_id,
                "channel_id": message.channel_id,
                "author_id": message.author_id,
                "author_name": message.author_name,
                "content": message.content,
                "reply_to_message_id": message.reply_to_message_id,
                "mentioned_user_ids": message.mentioned_user_ids,
                "created_at": message.created_at,
                "is_bot": message.is_bot,
            }
            statement = insert(ChatbotStoredMessage).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatbotStoredMessage.message_id],
                    set_={key: value for key, value in values.items() if key not in {"message_id", "created_at"}},
                )
            )

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

    async def get_latest_created_at(self, channel_id: int) -> datetime.datetime | None:
        """履歴の差分取得に使う、指定チャンネルの最新保存日時を返します。"""
        async with self._session_factory() as session:
            return await session.scalar(
                select(ChatbotStoredMessage.created_at)
                .where(ChatbotStoredMessage.channel_id == channel_id)
                .order_by(ChatbotStoredMessage.created_at.desc())
                .limit(1)
            )

    async def get_by_ids(self, message_ids: list[int]) -> list[ChatbotStoredMessage]:
        """指定IDの短期保存メッセージを時系列順に取得します。"""
        if not message_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotStoredMessage)
                .where(ChatbotStoredMessage.message_id.in_(message_ids))
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


class ChatbotMemoryExtractionQueueStore:
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


class ChatbotLongTermMemoryStore:
    """長期記憶と根拠投稿を保存します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, memory: LongTermMemoryInput) -> uuid.UUID:
        """長期記憶とその根拠投稿をまとめて保存します。"""
        now = datetime.datetime.now(datetime.UTC)
        expiration_days = {"ongoing": 90, "temporary": 7, "shared": 180}
        expires_at = now + datetime.timedelta(days=expiration_days[memory.kind]) if memory.kind in expiration_days else None
        async with self._session_factory.begin() as session:
            stored_memory = ChatbotLongTermMemory(
                target_user_id=memory.target_user_id,
                external_entity_name=memory.external_entity_name,
                target_resolution=memory.target_resolution,
                kind=memory.kind,
                content=memory.content,
                source_type=memory.source_type,
                status="active",
                is_sensitive=memory.is_sensitive,
                is_pinned=False,
                created_at=now,
                observed_at=memory.observed_at,
                expires_at=expires_at,
                embedding=memory.embedding,
            )
            session.add(stored_memory)
            await session.flush()
            for message_id, author_id, excerpt in zip(
                memory.evidence_message_ids,
                memory.evidence_author_ids,
                memory.evidence_excerpts,
                strict=True,
            ):
                session.add(
                    ChatbotLongTermMemoryEvidence(
                        memory_id=stored_memory.id,
                        message_id=message_id,
                        author_id=author_id,
                        excerpt=excerpt,
                        created_at=now,
                    )
                )
            return stored_memory.id

    async def get_active_for_target(
        self,
        target_user_id: int | None,
        external_entity_name: str | None,
    ) -> list[ChatbotLongTermMemory]:
        """訂正判定の比較対象となる同一対象の有効記憶を返します。"""
        async with self._session_factory() as session:
            target_condition = (
                ChatbotLongTermMemory.target_user_id == target_user_id
                if target_user_id is not None
                else ChatbotLongTermMemory.external_entity_name == external_entity_name
            )
            result = await session.execute(
                select(ChatbotLongTermMemory)
                .where(ChatbotLongTermMemory.status == "active", target_condition)
                .order_by(ChatbotLongTermMemory.created_at.desc())
                .limit(50)
            )
            return list(result.scalars().all())

    async def get_all_active_ordered(self) -> list[ChatbotLongTermMemory]:
        """既存記憶の一度限りの整理用に、有効記憶を古い順で返します。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotLongTermMemory)
                .where(ChatbotLongTermMemory.status == "active")
                .order_by(ChatbotLongTermMemory.observed_at, ChatbotLongTermMemory.created_at)
            )
            return list(result.scalars().all())

    async def get_review_records(self) -> list[LongTermMemoryReviewRecord]:
        """全長期記憶を管理確認向けの優先順で根拠とともに返します。"""
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory() as session:
            result = await session.execute(select(ChatbotLongTermMemory))
            memories = list(result.scalars().all())
            priority = {"conflicted": 0, "active": 1, "invalidated": 2, "superseded": 3}
            memories.sort(
                key=lambda memory: (
                    4
                    if memory.status == "active" and memory.expires_at and memory.expires_at <= now
                    else priority.get(memory.status, 5),
                    -(memory.observed_at or memory.created_at).timestamp(),
                )
            )
            evidence_result = await session.execute(
                select(
                    ChatbotLongTermMemoryEvidence.memory_id,
                    ChatbotLongTermMemoryEvidence.message_id,
                    ChatbotStoredMessage.author_name,
                    ChatbotLongTermMemoryEvidence.excerpt,
                    ChatbotStoredMessage.channel_id,
                ).outerjoin(
                    ChatbotStoredMessage,
                    ChatbotStoredMessage.message_id == ChatbotLongTermMemoryEvidence.message_id,
                )
            )
            evidences: dict[uuid.UUID, list[LongTermMemoryEvidenceRecord]] = {}
            for memory_id, message_id, author_name, excerpt, channel_id in evidence_result:
                evidences.setdefault(memory_id, []).append(
                    LongTermMemoryEvidenceRecord(message_id, author_name or str(message_id), excerpt, channel_id)
                )
            return [LongTermMemoryReviewRecord(memory, evidences.get(memory.id, [])) for memory in memories]

    async def apply_admin_updates(
        self,
        updates: list[LongTermMemoryUpdateInput],
        administrator_user_id: int,
    ) -> int:
        """検証・埋め込み生成済みの記憶変更を一括適用します。"""
        now = datetime.datetime.now(datetime.UTC)
        changed_count = 0
        async with self._session_factory.begin() as session:
            result = await session.execute(
                select(ChatbotLongTermMemory)
                .where(ChatbotLongTermMemory.id.in_([item.memory_id for item in updates]))
                .with_for_update()
            )
            memories = {memory.id: memory for memory in result.scalars().all()}
            if len(memories) != len(updates):
                msg = "存在しない記憶IDが含まれています"
                raise ValueError(msg)
            for item in updates:
                memory = memories[item.memory_id]
                before = self._admin_history_value(memory)
                if item.action == "invalidate":
                    memory.status = "invalidated"
                    memory.invalidated_at = now
                elif item.action in {"update", "activate"}:
                    memory.content = item.content
                    memory.target_user_id = item.target_user_id
                    memory.external_entity_name = item.external_entity_name
                    memory.target_resolution = item.target_resolution
                    memory.kind = item.kind
                    memory.source_type = item.source_type
                    memory.is_sensitive = item.is_sensitive
                    memory.expires_at = item.expires_at
                    if item.embedding is not None:
                        memory.embedding = item.embedding
                    if item.action == "activate":
                        memory.status = "active"
                        memory.invalidated_at = None
                        memory.superseded_by_memory_id = None
                        memory.conflict_group_id = None
                after = self._admin_history_value(memory)
                if before != after:
                    changed_count += 1
                    session.add(
                        ChatbotLongTermMemoryAdminHistory(
                            memory_id=memory.id,
                            administrator_user_id=administrator_user_id,
                            before_value=before,
                            after_value=after,
                            created_at=now,
                        )
                    )
        return changed_count

    def _admin_history_value(self, memory: ChatbotLongTermMemory) -> dict[str, object]:
        """管理者変更履歴へ保存する長期記憶の値を返します。"""
        return {
            "content": memory.content,
            "target_user_id": memory.target_user_id,
            "external_entity_name": memory.external_entity_name,
            "target_resolution": memory.target_resolution,
            "kind": memory.kind,
            "source_type": memory.source_type,
            "is_sensitive": memory.is_sensitive,
            "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            "status": memory.status,
            "superseded_by_memory_id": str(memory.superseded_by_memory_id) if memory.superseded_by_memory_id else None,
            "conflict_group_id": str(memory.conflict_group_id) if memory.conflict_group_id else None,
        }

    async def apply_reconciliation(
        self,
        reconciliation: MemoryReconciliationInput,
        *,
        new_memory_id: uuid.UUID | None,
    ) -> None:
        """モデルが選んだ既存記憶だけへ訂正・否定・競合状態を適用します。"""
        if reconciliation.action == "keep" or not reconciliation.existing_memory_ids:
            return
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            result = await session.execute(
                select(ChatbotLongTermMemory).where(
                    ChatbotLongTermMemory.id.in_(reconciliation.existing_memory_ids),
                    ChatbotLongTermMemory.status == "active",
                )
            )
            memories = list(result.scalars().all())
            if reconciliation.action == "supersede" and new_memory_id is not None:
                for memory in memories:
                    memory.status = "superseded"
                    memory.superseded_by_memory_id = new_memory_id
                    memory.invalidated_at = now
            elif reconciliation.action == "invalidate":
                for memory in memories:
                    memory.status = "invalidated"
                    memory.invalidated_at = now
            elif reconciliation.action == "conflict" and new_memory_id is not None:
                conflict_group_id = uuid.uuid4()
                for memory in memories:
                    memory.status = "conflicted"
                    memory.conflict_group_id = conflict_group_id
                new_memory = await session.get(ChatbotLongTermMemory, new_memory_id)
                if new_memory is not None:
                    new_memory.status = "conflicted"
                    new_memory.conflict_group_id = conflict_group_id
            session.add(
                ChatbotLongTermMemoryChange(
                    action=reconciliation.action,
                    new_memory_id=new_memory_id,
                    existing_memory_ids=[str(memory.id) for memory in memories],
                    evidence_message_ids=reconciliation.evidence_message_ids,
                    created_at=now,
                )
            )

    async def search(
        self,
        embedding: list[float],
        target_user_ids: set[int],
        maximum_cosine_distance: float,
        limit: int,
    ) -> list[ChatbotLongTermMemory]:
        """有効期限内の記憶を意味的な近さ順で取得します。"""
        now = datetime.datetime.now(datetime.UTC)
        cosine_distance = ChatbotLongTermMemory.embedding.cosine_distance(embedding)
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotLongTermMemory)
                .where(
                    ChatbotLongTermMemory.status == "active",
                    ChatbotLongTermMemory.embedding.is_not(None),
                    ChatbotLongTermMemory.is_sensitive.is_(False),
                    (ChatbotLongTermMemory.expires_at.is_(None)) | (ChatbotLongTermMemory.expires_at > now),
                    or_(
                        ChatbotLongTermMemory.target_user_id.is_(None),
                        ChatbotLongTermMemory.target_user_id.in_(target_user_ids),
                    ),
                    cosine_distance <= maximum_cosine_distance,
                )
                .order_by(cosine_distance)
                .limit(limit)
            )
            return list(result.scalars().all())


def normalize_member_alias(alias: str) -> str:
    """別名を比較・重複判定に使う表記へ揃えます。"""
    normalized_alias = " ".join(alias.casefold().split())
    for honorific in ("さん", "くん", "君", "ちゃん", "氏"):
        if normalized_alias.endswith(honorific) and len(normalized_alias) > len(honorific):
            return normalized_alias[: -len(honorific)].rstrip()
    return normalized_alias


def determine_member_alias_status(target_user_ids: set[int]) -> str:
    """同じ別名が一人だけを指す場合に限り名前解決を許可します。"""
    return "active" if len(target_user_ids) == 1 else "ambiguous"


def find_user_ids_by_member_alias(text_value: str, active_aliases: dict[str, int]) -> set[int]:
    """会話に含まれる有効な別名から対象メンバーIDを抽出します。"""
    normalized_text = normalize_member_alias(text_value)
    return {target_user_id for alias, target_user_id in active_aliases.items() if alias in normalized_text}


class ChatbotMemberAliasStore:
    """メンバー別名の確定状態と根拠を管理します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, alias_input: MemberAliasInput) -> None:
        """別名候補を保存し、衝突状態と既存の未解決記憶を更新します。"""
        normalized_alias = normalize_member_alias(alias_input.alias)
        if not normalized_alias:
            return
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            statement = (
                insert(ChatbotMemberAlias)
                .values(
                    alias=alias_input.alias.strip(),
                    normalized_alias=normalized_alias,
                    target_user_id=alias_input.target_user_id,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[
                        ChatbotMemberAlias.normalized_alias,
                        ChatbotMemberAlias.target_user_id,
                    ],
                    set_={"alias": alias_input.alias.strip(), "updated_at": now},
                )
                .returning(ChatbotMemberAlias.id)
            )
            alias_id = (await session.execute(statement)).scalar_one()
            for message_id, author_id, excerpt in zip(
                alias_input.evidence_message_ids,
                alias_input.evidence_author_ids,
                alias_input.evidence_excerpts,
                strict=True,
            ):
                await session.execute(
                    insert(ChatbotMemberAliasEvidence)
                    .values(
                        alias_id=alias_id,
                        message_id=message_id,
                        author_id=author_id,
                        excerpt=excerpt,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            ChatbotMemberAliasEvidence.alias_id,
                            ChatbotMemberAliasEvidence.message_id,
                        ]
                    )
                )
            await self._refresh_resolution(session, normalized_alias, now)

    async def get_active_aliases(self) -> dict[str, int]:
        """名前解決に利用できる一意な別名を返します。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotMemberAlias.normalized_alias, ChatbotMemberAlias.target_user_id).where(
                    ChatbotMemberAlias.status == "active"
                )
            )
            return dict(result.tuples().all())

    async def get_review_records(self) -> list[MemberAliasReviewRecord]:
        """全別名を曖昧なものから順に根拠付きで返します。"""
        async with self._session_factory() as session:
            alias_result = await session.execute(
                select(ChatbotMemberAlias).order_by(
                    (ChatbotMemberAlias.status == "ambiguous").desc(),
                    ChatbotMemberAlias.created_at.desc(),
                )
            )
            aliases = list(alias_result.scalars().all())
            evidence_result = await session.execute(
                select(
                    ChatbotMemberAliasEvidence.alias_id,
                    ChatbotMemberAliasEvidence.message_id,
                    ChatbotStoredMessage.author_name,
                    ChatbotMemberAliasEvidence.excerpt,
                    ChatbotStoredMessage.channel_id,
                ).outerjoin(
                    ChatbotStoredMessage,
                    ChatbotStoredMessage.message_id == ChatbotMemberAliasEvidence.message_id,
                )
            )
            evidences_by_alias: dict[uuid.UUID, list[MemberAliasEvidenceRecord]] = {}
            for alias_id, message_id, author_name, excerpt, channel_id in evidence_result:
                evidences_by_alias.setdefault(alias_id, []).append(
                    MemberAliasEvidenceRecord(
                        message_id=message_id,
                        author_name=author_name or str(message_id),
                        excerpt=excerpt,
                        channel_id=channel_id,
                    )
                )
            return [
                MemberAliasReviewRecord(
                    id=alias.id,
                    alias=alias.alias,
                    normalized_alias=alias.normalized_alias,
                    target_user_id=alias.target_user_id,
                    status=alias.status,
                    created_at=alias.created_at,
                    updated_at=alias.updated_at,
                    evidences=evidences_by_alias.get(alias.id, []),
                )
                for alias in aliases
            ]

    async def apply_updates(
        self,
        updates: list[MemberAliasUpdateInput],
        administrator_user_id: int,
    ) -> None:
        """検証済みのExcel変更を一つのトランザクションで適用します。"""
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            result = await session.execute(
                select(ChatbotMemberAlias)
                .where(ChatbotMemberAlias.id.in_([update_input.alias_id for update_input in updates]))
                .with_for_update()
            )
            aliases_by_id = {alias.id: alias for alias in result.scalars().all()}
            if len(aliases_by_id) != len(updates):
                msg = "存在しない別名IDが含まれています"
                raise ValueError(msg)
            affected_normalized_aliases: set[str] = set()
            for update_input in updates:
                alias = aliases_by_id[update_input.alias_id]
                before_value = self._alias_history_value(alias)
                affected_normalized_aliases.add(alias.normalized_alias)
                if update_input.action == "invalidate":
                    alias.status = "invalidated"
                    alias.invalidated_at = now
                else:
                    normalized_alias = normalize_member_alias(update_input.alias)
                    if not normalized_alias:
                        msg = "別名を空にはできません"
                        raise ValueError(msg)
                    alias.alias = update_input.alias.strip()
                    alias.normalized_alias = normalized_alias
                    if update_input.action == "change_target":
                        if update_input.target_user_id is None:
                            msg = "変更後の対象者がありません"
                            raise ValueError(msg)
                        alias.target_user_id = update_input.target_user_id
                        alias.status = "active"
                    alias.invalidated_at = None
                    affected_normalized_aliases.add(normalized_alias)
                alias.updated_at = now
                after_value = self._alias_history_value(alias)
                if before_value != after_value:
                    session.add(
                        ChatbotMemberAliasHistory(
                            alias_id=alias.id,
                            administrator_user_id=administrator_user_id,
                            before_value=before_value,
                            after_value=after_value,
                            created_at=now,
                        )
                    )
            await session.flush()
            for normalized_alias in affected_normalized_aliases:
                await self._refresh_resolution(session, normalized_alias, now)

    def _alias_history_value(self, alias: ChatbotMemberAlias) -> dict[str, object]:
        """履歴へ保存する別名の管理対象項目を返します。"""
        return {
            "alias": alias.alias,
            "normalized_alias": alias.normalized_alias,
            "target_user_id": alias.target_user_id,
            "status": alias.status,
        }

    async def _refresh_resolution(
        self,
        session: AsyncSession,
        normalized_alias: str,
        now: datetime.datetime,
    ) -> None:
        """同じ別名の衝突状態を揃え、一意なら未解決記憶も結び直します。"""
        result = await session.execute(
            select(ChatbotMemberAlias).where(
                ChatbotMemberAlias.normalized_alias == normalized_alias,
                ChatbotMemberAlias.status != "invalidated",
            )
        )
        aliases = list(result.scalars().all())
        target_user_ids = {alias.target_user_id for alias in aliases}
        status = determine_member_alias_status(target_user_ids)
        for alias in aliases:
            alias.status = status
            alias.updated_at = now
        if status != "active":
            return
        target_user_id = next(iter(target_user_ids))
        unresolved_result = await session.execute(
            select(ChatbotLongTermMemory).where(
                ChatbotLongTermMemory.target_resolution == "unresolved",
                ChatbotLongTermMemory.external_entity_name.is_not(None),
            )
        )
        for memory in unresolved_result.scalars():
            if memory.external_entity_name and normalize_member_alias(memory.external_entity_name) == normalized_alias:
                memory.target_user_id = target_user_id
                memory.external_entity_name = None
                memory.target_resolution = "member"


async def create_chatbot_tables(engine: AsyncEngine) -> None:
    """chatbotスキーマのテーブルと意味検索用拡張を作成します。"""
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS chatbot.chatbot_long_term_memories "
                "ADD COLUMN IF NOT EXISTS superseded_by_memory_id UUID, "
                "ADD COLUMN IF NOT EXISTS conflict_group_id UUID, "
                "ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ"
            )
        )
    await create_tables_for(engine, ChatbotBase.metadata, schema=CHATBOT_DATABASE_SCHEMA)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE chatbot.chatbot_long_term_memories AS memory SET observed_at = source.observed_at "
                "FROM (SELECT evidence.memory_id, MIN(message.created_at) AS observed_at "
                "FROM chatbot.chatbot_long_term_memory_evidences AS evidence "
                "JOIN chatbot.chatbot_stored_messages AS message ON message.message_id = evidence.message_id "
                "GROUP BY evidence.memory_id) AS source "
                "WHERE memory.id = source.memory_id AND memory.observed_at IS NULL"
            )
        )
        await connection.execute(
            text("UPDATE chatbot.chatbot_long_term_memories SET observed_at = created_at WHERE observed_at IS NULL")
        )
