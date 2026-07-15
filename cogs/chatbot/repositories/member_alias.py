import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import BigInteger, ForeignKey, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase
from .long_term_memory import ChatbotLongTermMemory
from .short_term_message import ChatbotStoredMessage


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


def find_member_aliases(text_value: str, active_aliases: dict[str, int]) -> dict[str, int]:
    """会話に実際に含まれる有効な別名と対象メンバーIDを返します。"""
    normalized_text = normalize_member_alias(text_value)
    return {alias: target_user_id for alias, target_user_id in active_aliases.items() if alias in normalized_text}


class ChatbotMemberAliasRepository:
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
