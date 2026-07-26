import datetime
from dataclasses import dataclass

from sqlalchemy import BigInteger, Boolean, Text, delete, or_, select, update
from sqlalchemy.dialects.postgresql import TIMESTAMP, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase
from .member_alias import (
    ChatbotMemberAlias,
    ChatbotMemberAliasEvidence,
    determine_member_alias_status,
    normalize_member_alias,
)


class ChatbotMemoryDocument(ChatbotBase):
    """継続的に書き換えるChatbotの長期記憶文書。"""

    __tablename__ = "chatbot_memory_documents"

    document_key = mapped_column(Text, primary_key=True)
    document_type = mapped_column(Text, nullable=False, index=True)
    target_user_id = mapped_column(BigInteger, nullable=True, index=True)
    content = mapped_column(Text, nullable=False)
    updated_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ChatbotMemoryProcessingCursor(ChatbotBase):
    """チャンネルごとの長期記憶処理位置と最終人間投稿。"""

    __tablename__ = "chatbot_memory_processing_cursors"

    channel_id = mapped_column(BigInteger, primary_key=True)
    last_processed_message_id = mapped_column(BigInteger, nullable=True)
    last_human_message_at = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    updated_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ChatbotMemoryUpdateJob(ChatbotBase):
    """会話単位で直列処理する長期記憶文書の更新ジョブ。"""

    __tablename__ = "chatbot_memory_update_jobs"

    channel_id = mapped_column(BigInteger, primary_key=True)
    end_message_id = mapped_column(BigInteger, nullable=False)
    trigger = mapped_column(Text, nullable=False)
    wait_for_attachments = mapped_column(Boolean, nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    attempt_count = mapped_column(BigInteger, nullable=False, default=0)
    queued_at = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)


@dataclass(frozen=True, slots=True)
class MemoryDocumentInput:
    """一括保存する長期記憶文書。"""

    document_key: str
    document_type: str
    target_user_id: int | None
    content: str


@dataclass(frozen=True, slots=True)
class MemoryAliasInput:
    """文書更新と同じトランザクションで保存する別名。"""

    alias: str
    target_user_id: int
    evidence_message_ids: list[int]
    evidence_author_ids: list[int]
    evidence_excerpts: list[str]


class ChatbotMemoryDocumentRepository:
    """長期記憶文書、処理位置、更新ジョブを永続化します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_all(self) -> list[ChatbotMemoryDocument]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotMemoryDocument).order_by(
                    ChatbotMemoryDocument.document_type,
                    ChatbotMemoryDocument.target_user_id,
                )
            )
            return list(result.scalars().all())

    async def get_for_users(self, user_ids: set[int]) -> list[ChatbotMemoryDocument]:
        async with self._session_factory() as session:
            conditions = [ChatbotMemoryDocument.document_type.in_(("bot", "shared"))]
            if user_ids:
                conditions.append(
                    (ChatbotMemoryDocument.document_type == "person") & ChatbotMemoryDocument.target_user_id.in_(user_ids)
                )
            result = await session.execute(
                select(ChatbotMemoryDocument).where(or_(*conditions)).order_by(ChatbotMemoryDocument.document_key)
            )
            return list(result.scalars().all())

    async def get_cursor(self, channel_id: int) -> ChatbotMemoryProcessingCursor | None:
        async with self._session_factory() as session:
            return await session.get(ChatbotMemoryProcessingCursor, channel_id)

    async def get_cursors(self) -> list[ChatbotMemoryProcessingCursor]:
        async with self._session_factory() as session:
            result = await session.execute(select(ChatbotMemoryProcessingCursor))
            return list(result.scalars().all())

    async def note_human_message(self, channel_id: int, created_at: datetime.datetime) -> None:
        now = datetime.datetime.now(datetime.UTC)
        values = {
            "channel_id": channel_id,
            "last_human_message_at": created_at,
            "updated_at": now,
        }
        async with self._session_factory.begin() as session:
            statement = insert(ChatbotMemoryProcessingCursor).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatbotMemoryProcessingCursor.channel_id],
                    set_={key: value for key, value in values.items() if key != "channel_id"},
                )
            )

    async def enqueue(
        self,
        channel_id: int,
        end_message_id: int,
        trigger: str,
        *,
        wait_for_attachments: bool,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            existing = await session.get(
                ChatbotMemoryUpdateJob,
                channel_id,
                with_for_update=True,
            )
            if existing is None:
                session.add(
                    ChatbotMemoryUpdateJob(
                        channel_id=channel_id,
                        end_message_id=end_message_id,
                        trigger=trigger,
                        wait_for_attachments=wait_for_attachments,
                        status="pending",
                        attempt_count=0,
                        queued_at=now,
                    )
                )
            elif existing.status == "failed":
                existing.end_message_id = end_message_id
                existing.trigger = trigger
                existing.wait_for_attachments = wait_for_attachments
                existing.status = "pending"
                existing.attempt_count = 0
                existing.queued_at = now

    async def claim_next(self) -> ChatbotMemoryUpdateJob | None:
        async with self._session_factory.begin() as session:
            result = await session.execute(
                select(ChatbotMemoryUpdateJob)
                .where(ChatbotMemoryUpdateJob.status == "pending")
                .order_by(ChatbotMemoryUpdateJob.queued_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            if job is not None:
                job.status = "processing"
                job.attempt_count += 1
            return job

    async def restore_interrupted(self) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(ChatbotMemoryUpdateJob).where(ChatbotMemoryUpdateJob.status == "processing").values(status="pending")
            )

    async def mark_failed(self, channel_id: int) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(ChatbotMemoryUpdateJob).where(ChatbotMemoryUpdateJob.channel_id == channel_id).values(status="failed")
            )

    async def complete(
        self,
        channel_id: int,
        end_message_id: int,
        documents: list[MemoryDocumentInput],
        aliases: list[MemoryAliasInput],
    ) -> None:
        """文書群、別名、カーソルを一つのトランザクションで更新します。"""
        now = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            for document in documents:
                values = {
                    "document_key": document.document_key,
                    "document_type": document.document_type,
                    "target_user_id": document.target_user_id,
                    "content": document.content,
                    "updated_at": now,
                }
                statement = insert(ChatbotMemoryDocument).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[ChatbotMemoryDocument.document_key],
                        set_={key: value for key, value in values.items() if key != "document_key"},
                    )
                )
            for alias_input in aliases:
                normalized_alias = normalize_member_alias(alias_input.alias)
                if not normalized_alias:
                    continue
                alias_statement = (
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
                alias_id = (await session.execute(alias_statement)).scalar_one()
                for message_id, author_id, excerpt in zip(
                    alias_input.evidence_message_ids,
                    alias_input.evidence_author_ids,
                    alias_input.evidence_excerpts,
                    strict=True,
                ):
                    evidence_statement = insert(ChatbotMemberAliasEvidence).values(
                        alias_id=alias_id,
                        message_id=message_id,
                        author_id=author_id,
                        excerpt=excerpt,
                        created_at=now,
                    )
                    await session.execute(
                        evidence_statement.on_conflict_do_nothing(
                            index_elements=[
                                ChatbotMemberAliasEvidence.alias_id,
                                ChatbotMemberAliasEvidence.message_id,
                            ]
                        )
                    )
                target_result = await session.execute(
                    select(ChatbotMemberAlias.target_user_id).where(
                        ChatbotMemberAlias.normalized_alias == normalized_alias,
                        ChatbotMemberAlias.invalidated_at.is_(None),
                    )
                )
                status = determine_member_alias_status(set(target_result.scalars().all()))
                await session.execute(
                    update(ChatbotMemberAlias)
                    .where(
                        ChatbotMemberAlias.normalized_alias == normalized_alias,
                        ChatbotMemberAlias.invalidated_at.is_(None),
                    )
                    .values(status=status, updated_at=now)
                )
            cursor_statement = insert(ChatbotMemoryProcessingCursor).values(
                channel_id=channel_id,
                last_processed_message_id=end_message_id,
                updated_at=now,
            )
            await session.execute(
                cursor_statement.on_conflict_do_update(
                    index_elements=[ChatbotMemoryProcessingCursor.channel_id],
                    set_={"last_processed_message_id": end_message_id, "updated_at": now},
                )
            )
            await session.execute(delete(ChatbotMemoryUpdateJob).where(ChatbotMemoryUpdateJob.channel_id == channel_id))
