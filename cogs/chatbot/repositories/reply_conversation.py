from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.models.reply_conversation import ConversationMessage, ReplyConversation

from .environment import DatabaseEnvironment


class ReplyConversationRepository:
    """既存の保存領域にBot別の会話スナップショットを期限なしで保存します。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], namespace: str) -> None:
        """保存に使うセッション生成処理とBotごとの名前空間を設定します。"""
        self._session_factory = session_factory
        self.namespace = namespace

    def _key(self, kind: str, message_id: int) -> str:
        """Bot・保存種別・投稿IDを組み合わせた保存キーを返します。"""
        return f"reply_conversation:{self.namespace}:{kind}:{message_id}"

    async def _get(self, kind: str, message_id: int) -> str | None:
        """保存済みJSONを取得し、未保存ならNoneを返します。"""
        async with self._session_factory() as session:
            return await session.scalar(
                select(DatabaseEnvironment.value).where(
                    DatabaseEnvironment.key == self._key(kind, message_id),
                )
            )

    async def _save(self, kind: str, message_id: int, value: str) -> None:
        """同じキーの初回保存内容を上書きせず、未保存の場合だけJSONを保存します。"""
        async with self._session_factory.begin() as session:
            await session.execute(
                insert(DatabaseEnvironment)
                .values(
                    key=self._key(kind, message_id),
                    value=value,
                )
                .on_conflict_do_nothing(index_elements=[DatabaseEnvironment.key])
            )

    async def get_message(self, message_id: int) -> ConversationMessage | None:
        """会話に取り込んだ投稿を復元し、未保存ならNoneを返します。"""
        value = await self._get("message", message_id)
        return ConversationMessage.model_validate_json(value) if value else None

    async def save_message(self, message: ConversationMessage) -> ConversationMessage:
        """投稿を保存し、既に保存されている場合は初回の内容を返します。"""
        await self._save("message", message.message_id, message.model_dump_json())
        return await self.get_message(message.message_id) or message

    async def get_turn(self, message_id: int) -> ReplyConversation | None:
        """Botの返信に対応する会話の継続点を取得します。"""
        value = await self._get("turn", message_id)
        return ReplyConversation.model_validate_json(value) if value else None

    async def save_turn(self, message_id: int, conversation: ReplyConversation) -> None:
        """Botの返信に会話の継続点を結び付け、初回の内容を保存します。"""
        await self._save("turn", message_id, conversation.model_dump_json())
