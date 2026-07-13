import datetime
from dataclasses import dataclass

from sqlalchemy import BigInteger, Boolean, ForeignKey, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase


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


class ChatbotStoredReactionSnapshot(ChatbotBase):
    """メッセージ単位で保存するリアクションの最新スナップショット。"""

    __tablename__ = "chatbot_stored_reaction_snapshots"

    message_id = mapped_column(
        BigInteger,
        ForeignKey("chatbot.chatbot_stored_messages.message_id"),
        primary_key=True,
    )
    reactions = mapped_column(JSONB, nullable=False)
    updated_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)


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
class StoredReactionSnapshotInput:
    """短期文脈として保存するリアクションの最新状態。"""

    message_id: int
    reactions: list[dict[str, object]]


class ChatbotShortTermMessageRepository:
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
            await session.execute(
                delete(ChatbotStoredReactionSnapshot).where(ChatbotStoredReactionSnapshot.message_id.in_(expired_message_ids))
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
            await session.execute(
                delete(ChatbotStoredReactionSnapshot).where(ChatbotStoredReactionSnapshot.message_id == message_id)
            )
            await session.execute(delete(ChatbotStoredMessage).where(ChatbotStoredMessage.message_id == message_id))

    async def contains(self, message_id: int) -> bool:
        """指定メッセージが短期保存対象に含まれるか確認します。"""
        async with self._session_factory() as session:
            return await session.get(ChatbotStoredMessage, message_id) is not None

    async def save_reactions(self, snapshot: StoredReactionSnapshotInput) -> None:
        """メッセージのリアクションを最新スナップショットへ置き換えます。"""
        await self.save_reaction_snapshots([snapshot])

    async def save_reaction_snapshots(self, snapshots: list[StoredReactionSnapshotInput]) -> None:
        """複数メッセージのリアクションを1トランザクションで置き換えます。"""
        if not snapshots:
            return
        updated_at = datetime.datetime.now(datetime.UTC)
        async with self._session_factory.begin() as session:
            for snapshot in snapshots:
                values = {
                    "message_id": snapshot.message_id,
                    "reactions": snapshot.reactions,
                    "updated_at": updated_at,
                }
                statement = insert(ChatbotStoredReactionSnapshot).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[ChatbotStoredReactionSnapshot.message_id],
                        set_={"reactions": values["reactions"], "updated_at": values["updated_at"]},
                    )
                )

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

    async def get_reaction_snapshots(self, message_ids: list[int]) -> list[ChatbotStoredReactionSnapshot]:
        """指定メッセージに紐づくリアクションスナップショットを取得します。"""
        if not message_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatbotStoredReactionSnapshot).where(ChatbotStoredReactionSnapshot.message_id.in_(message_ids))
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
