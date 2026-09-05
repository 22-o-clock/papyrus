import datetime
import json
from dataclasses import dataclass, field
from logging import getLogger
from typing import Literal, Self, cast

import dateutil
import discord
import tiktoken
from discord import Message
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .constants import (
    CONVERSATION_INACTIVITY_SECONDS,
    SHORT_TERM_MEMORY_RETAINED_TOKENS,
)
from .observability import observe_chatbot_api_call
from .prompt import load_prompt
from .services.prompt_context import omit_empty_values

logger = getLogger(__name__)

MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS = load_prompt("long_term_memory_update.md")
MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS = load_prompt("long_term_memory_shorten.md")
MEMORY_DOCUMENT_UPDATE_MODEL = "gpt-5.6-luna"
LOCAL_TIMEZONE = dateutil.tz.gettz("Asia/Tokyo")


def _coerce_int(value: object) -> int:
    """DBから復元したJSON値を整数へ安全に変換します。"""
    if isinstance(value, int | str):
        return int(value)
    return 0


@dataclass
class AttachmentInMemory:
    """短期文脈で参照する添付ファイルの解析情報。"""

    attachment_id: int
    filename: str
    kind: str
    analysis_status: str
    summary: str | None = None
    important_text: str | None = None

    def to_dict(self) -> dict[str, object]:
        """プロンプトに渡す添付情報を辞書形式で返します。"""
        result: dict[str, object] = {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "kind": self.kind,
            "analysis_status": self.analysis_status,
        }
        if self.analysis_status == "completed":
            result["summary"] = self.summary or ""
            result["important_text"] = self.important_text or ""
        return result


@dataclass
class ReactionUserInMemory:
    """リアクションしたDiscordユーザーの取得時点の情報。"""

    user_id: int
    display_name: str
    is_bot: bool

    def to_dict(self) -> dict[str, object]:
        """発言生成用のユーザー情報を辞書形式で返します。"""
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "is_bot": self.is_bot,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Self:
        """DBのJSON表現からリアクションユーザーを復元します。"""
        return cls(
            user_id=_coerce_int(value["user_id"]),
            display_name=str(value["display_name"]),
            is_bot=bool(value["is_bot"]),
        )


@dataclass
class ReactionInMemory:
    """短期文脈で参照するDiscordリアクションのスナップショット。"""

    emoji_name: str
    emoji_id: int | None
    animated: bool
    reaction_type: Literal["normal", "burst"]
    count: int
    reactors: list[ReactionUserInMemory] = field(default_factory=list)
    reactors_truncated: bool = False
    reactors_incomplete: bool = False

    def to_dict(self) -> dict[str, object]:
        """発言生成用のリアクション情報を辞書形式で返します。"""
        return {
            "emoji_name": self.emoji_name,
            "emoji_id": self.emoji_id,
            "animated": self.animated,
            "reaction_type": self.reaction_type,
            "count": self.count,
            "reactors": [reactor.to_dict() for reactor in self.reactors],
            "reactors_truncated": self.reactors_truncated,
            "reactors_incomplete": self.reactors_incomplete,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Self:
        """DBのJSON表現からリアクションを復元します。"""
        raw_reactors = value.get("reactors", [])
        reactors = (
            [
                ReactionUserInMemory.from_dict(cast("dict[str, object]", reactor))
                for reactor in raw_reactors
                if isinstance(reactor, dict)
            ]
            if isinstance(raw_reactors, list)
            else []
        )
        reaction_type = "burst" if value.get("reaction_type") == "burst" else "normal"
        raw_emoji_id = value.get("emoji_id")
        return cls(
            emoji_name=str(value.get("emoji_name", "")),
            emoji_id=int(raw_emoji_id) if isinstance(raw_emoji_id, int | str) else None,
            animated=bool(value.get("animated", False)),
            reaction_type=reaction_type,
            count=_coerce_int(value.get("count", 0)),
            reactors=reactors,
            reactors_truncated=bool(value.get("reactors_truncated", False)),
            reactors_incomplete=bool(value.get("reactors_incomplete", False)),
        )


@dataclass
class MessageInMemory:
    """短期記憶内に保存されるメッセージを表すデータクラス。

    Attributes:
        message_id: メッセージのID
        author_id: メッセージの送信者のDiscordユーザーID
        author_name: メッセージの送信者の表示名
        content: メッセージの内容
        reply_to_message_id: 返信先のDiscordメッセージID
        mentioned_user_ids: メンションされたDiscordユーザーIDの一覧
        timestamp: メッセージが作成された日時
        is_forwarded: 転送された第三者のメッセージか
        is_stale_context: 長時間前の参考情報としてのみ使うメッセージか
        attachments: 添付の解析状態と、完了済みの場合は要約・重要テキスト
        reactions: 発言生成時に参照するリアクション情報

    """

    message_id: int
    author_id: int
    author_name: str
    content: str
    reply_to_message_id: int | None
    mentioned_user_ids: list[int]
    timestamp: datetime.datetime
    is_bot: bool = False
    is_forwarded: bool = False
    is_stale_context: bool = False
    embeds: list[dict[str, object]] = field(default_factory=list)
    attachments: list[AttachmentInMemory] = field(default_factory=list)
    reactions: list[ReactionInMemory] = field(default_factory=list)

    def to_dict(self, *, include_reactions: bool = True) -> dict[str, object]:
        """プロンプト作成に用いる要素のみを辞書形式で出力します。

        Returns:
            人物と返信先をDiscord IDで識別できる辞書

        """
        result: dict[str, object] = {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "reply_to_message_id": self.reply_to_message_id,
            "mentioned_user_ids": self.mentioned_user_ids,
            "timestamp": self.timestamp.astimezone(LOCAL_TIMEZONE).isoformat(),
            "is_stale_context": self.is_stale_context,
            "embeds": self.embeds,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }
        if include_reactions:
            result["reactions"] = [reaction.to_dict() for reaction in self.reactions]
        return result

    def to_prompt_dict(self) -> dict[str, object]:
        """既定値と空要素を省いた、モデル入力用の短い辞書を返します。"""
        result: dict[str, object] = {
            "i": self.message_id,
            "a": self.author_id,
            "t": self.timestamp.astimezone(LOCAL_TIMEZONE).isoformat(timespec="minutes"),
        }
        content = self.content
        if content:
            result["c"] = content
        if self.reply_to_message_id is not None:
            result["r"] = self.reply_to_message_id
        if self.mentioned_user_ids:
            result["u"] = self.mentioned_user_ids
        if self.is_forwarded:
            result["f"] = True
        if self.is_stale_context:
            result["s"] = True
        if self.embeds:
            result["e"] = [omit_empty_values(embed) for embed in self.embeds]
        if self.attachments:
            result["x"] = [omit_empty_values(attachment.to_dict()) for attachment in self.attachments]
        if self.reactions:
            result["q"] = [omit_empty_values(reaction.to_dict()) for reaction in self.reactions]
        return result


class ShortTermMemory:
    """短期記憶を管理するクラス。メッセージの履歴をトークン数の制限内で保持します。"""

    def __init__(self, model: str = "gpt-5-") -> None:
        """短期メモリを初期化します。

        Args:
            model: トークンカウント用のtiktokenのモデル名 (デフォルト: "gpt-5-")

        """
        self.memory: list[MessageInMemory] = []
        self.encoding = tiktoken.encoding_for_model(model)

    async def append(self, message: Message) -> None:
        """メッセージを短期記憶に追加します。

        Args:
            message: 追加するDiscordメッセージ

        """
        # 1. デフォルト値の設定

        message_id = message.id
        author_id = message.author.id
        author_name = message.author.display_name
        content = message.clean_content
        reply_to_message_id = None
        mentioned_user_ids = [user.id for user in message.mentions]
        timestamp = message.created_at
        embeds = [
            {
                "type": embed.type,
                "url": embed.url,
                "provider": embed.provider.name if embed.provider is not None else None,
                "author": embed.author.name if embed.author is not None else None,
                "title": embed.title,
                "description": embed.description,
                "fields": [{"name": embed_field.name, "value": embed_field.value} for embed_field in embed.fields],
                "footer": embed.footer.text if embed.footer is not None else None,
                "timestamp": embed.timestamp.isoformat() if embed.timestamp is not None else None,
            }
            for embed in getattr(message, "embeds", [])
        ]

        # 2. reply_to に関する特殊処理

        is_forwarded = bool(message.message_snapshots)
        if is_forwarded:
            content = message.message_snapshots[0].content

        if message.type == discord.MessageType.reply and message.reference and message.reference.message_id:
            reply_to_message_id = message.reference.message_id
        elif message.type == discord.MessageType.reply:
            logger.warning(
                "Message is a reply but referenced message not found (ref_id=%s, channel_id=%s, guild_id=%s)",
                message.reference.message_id if message.reference else None,
                message.channel.id,
                message.guild.id if message.guild else None,
            )

        # 4. メモリへの追加

        self.memory.append(
            MessageInMemory(
                message_id=message_id,
                author_id=author_id,
                author_name=author_name,
                content=content,
                reply_to_message_id=reply_to_message_id,
                mentioned_user_ids=mentioned_user_ids,
                timestamp=timestamp,
                is_bot=bool(getattr(message.author, "bot", False)),
                is_forwarded=is_forwarded,
                embeds=embeds,
            )
        )

        # 5. メモリ内のメッセージを日時順にソート
        self.memory.sort(key=lambda m: m.timestamp)

        logger.debug("Current messages in memory: %s messages", len(self.memory))

    def to_json(self) -> str:
        """保持する短期記憶のトークン計数用にコンパクトJSONを返します。"""
        return json.dumps(
            {
                "a": {str(message.author_id): message.author_name for message in self.memory},
                "m": [message.to_prompt_dict() for message in self.memory],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def forget(self, maximum_token: int = SHORT_TERM_MEMORY_RETAINED_TOKENS) -> None:
        """メモリ内のメッセージを古い順に削除して、トークン数を制限以下に保ちます。

        Args:
            maximum_token: 内部で保持する最大トークン数

        """
        while self.memory:
            token_count = len(self.encoding.encode(self.to_json()))

            if token_count <= maximum_token:
                break

            self.memory.pop(0)

        logger.debug(
            "Current messages in memory: %s tokens",
            len(self.encoding.encode(self.to_json())),
        )

        logger.debug("Current messages in memory after pruning: %s messages", len(self.memory))

    def contains_message(self, message_id: int) -> bool:
        """指定されたDiscordメッセージが現在の短期記憶に含まれるか確認します。"""
        return any(message.message_id == message_id for message in self.memory)

    def get_author_id(self, message_id: int) -> int | None:
        """指定されたDiscordメッセージの発言者IDを取得します。"""
        for message in self.memory:
            if message.message_id == message_id:
                return message.author_id
        return None

    def get_message(self, message_id: int) -> MessageInMemory | None:
        """指定されたDiscordメッセージの短期記憶データを取得します。"""
        return next((message for message in self.memory if message.message_id == message_id), None)

    def remove(self, message_id: int) -> None:
        """指定されたDiscordメッセージを短期記憶から除去します。"""
        self.memory = [message for message in self.memory if message.message_id != message_id]

    def set_attachment_analysis(
        self,
        message_id: int,
        attachment: AttachmentInMemory,
    ) -> None:
        """添付の解析状態を、対応するメッセージの文脈情報へ反映します。"""
        message = self.get_message(message_id)
        if message is None:
            return
        message.attachments = [
            existing_attachment
            for existing_attachment in message.attachments
            if existing_attachment.attachment_id != attachment.attachment_id
        ]
        message.attachments.append(attachment)

    def set_reactions(self, message_id: int, reactions: list[ReactionInMemory]) -> None:
        """対象メッセージのリアクションを最新スナップショットへ置き換えます。"""
        message = self.get_message(message_id)
        if message is not None:
            message.reactions = reactions

    def reset_for_new_conversation(self) -> None:
        """直前の投稿だけを古い参考情報として残し、現在の会話文脈をリセットします。"""
        if not self.memory:
            return

        last_message = self.memory[-1]
        last_message.is_stale_context = True
        self.memory = [last_message]

    def restore(self, messages: list[MessageInMemory]) -> None:
        """DB履歴から最後の12時間空白を再現し、現在の会話を復元します。"""
        restored_messages: list[MessageInMemory] = []
        last_human_message_timestamp: datetime.datetime | None = None
        for message in sorted(messages, key=lambda candidate: candidate.timestamp):
            if (
                not message.is_bot
                and last_human_message_timestamp is not None
                and message.timestamp - last_human_message_timestamp
                >= datetime.timedelta(seconds=CONVERSATION_INACTIVITY_SECONDS)
            ):
                if restored_messages:
                    previous_message = restored_messages[-1]
                    previous_message.is_stale_context = True
                    restored_messages = [previous_message]
                else:
                    restored_messages = []
            restored_messages.append(message)
            if not message.is_bot:
                last_human_message_timestamp = message.timestamp
        self.memory = restored_messages
        self.forget()


class MemberAliasCandidate(BaseModel):
    """会話全体から抽出したサーバーメンバーの別名候補。"""

    alias: str
    target_user_id: int
    evidence_message_ids: list[int]


class MemoryDocumentUpdate(BaseModel):
    """モデルが返す長期記憶Markdown文書の完成形。"""

    document_key: str
    document_type: Literal["person", "bot", "shared"]
    target_user_id: int | None
    content: str


class MemoryDocumentUpdateResult(BaseModel):
    """一回の会話分析で更新する文書と別名の集合。"""

    updates: list[MemoryDocumentUpdate] = Field(default_factory=list)
    aliases: list[MemberAliasCandidate] = Field(default_factory=list)


class MemoryDocumentShortenResult(BaseModel):
    """入力順に短縮した長期記憶Markdown本文。"""

    contents: list[str] = Field(default_factory=list)


class MemoryDocumentUpdater:
    """会話単位で長期記憶Markdown文書を更新します。"""

    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    async def update(
        self,
        payload: dict[str, object],
    ) -> MemoryDocumentUpdateResult:
        """既存文書と会話を渡し、変更された文書だけを受け取ります。"""
        operation = "memory_document_update"
        channel_conversations = payload.get("channel_conversations")
        item_count = len(channel_conversations) if isinstance(channel_conversations, list) else 1
        response = await observe_chatbot_api_call(
            operation,
            MEMORY_DOCUMENT_UPDATE_MODEL,
            self.client.responses.parse(
                model=MEMORY_DOCUMENT_UPDATE_MODEL,
                reasoning={"effort": "medium"},
                instructions=MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                text_format=MemoryDocumentUpdateResult,
                metadata={"operation": operation},
            ),
            item_count=item_count,
        )
        if response.output_parsed is None:
            logger.warning("Failed to parse memory document update response")
            return MemoryDocumentUpdateResult()
        return response.output_parsed

    async def shorten(self, payload: dict[str, object]) -> MemoryDocumentShortenResult:
        """対象文書の本文だけを短縮し、入力順に受け取ります。"""
        operation = "memory_document_shorten"
        documents = payload.get("documents")
        item_count = len(documents) if isinstance(documents, list) else 1
        response = await observe_chatbot_api_call(
            operation,
            MEMORY_DOCUMENT_UPDATE_MODEL,
            self.client.responses.parse(
                model=MEMORY_DOCUMENT_UPDATE_MODEL,
                reasoning={"effort": "medium"},
                instructions=MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                text_format=MemoryDocumentShortenResult,
                metadata={"operation": operation},
            ),
            item_count=item_count,
        )
        if response.output_parsed is None:
            logger.warning("Failed to parse memory document shorten response")
            return MemoryDocumentShortenResult()
        return response.output_parsed
