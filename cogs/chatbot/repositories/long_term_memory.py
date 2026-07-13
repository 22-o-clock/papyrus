import datetime
import uuid
from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, ForeignKey, Text, or_, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase
from .short_term_message import ChatbotStoredMessage

EMBEDDING_DIMENSIONS = 3072


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


class ChatbotLongTermMemoryRepository:
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
