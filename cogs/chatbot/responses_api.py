import datetime
import json
from dataclasses import dataclass
from enum import StrEnum
from logging import getLogger
from typing import Any, Self

import dateutil
import discord
import tiktoken
from discord import Message
from openai import AsyncOpenAI
from pydantic import BaseModel, model_validator

from .channel_roles import ChannelRole
from .prompt import draft_generator_prompt, response_styler_prompt

logger = getLogger(__name__)

DRAFT_GENERATOR_MODEL = "gpt-5.2"
STYLER_MODEL = "gpt-5.4-mini"
LOCAL_TIMEZONE = dateutil.tz.gettz("Asia/Tokyo")


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
        image_url: メッセージに含まれる画像のURL (存在する場合)
        pdf_url: メッセージに含まれるPDFのURL (存在する場合)

    """

    message_id: int
    author_id: int
    author_name: str
    content: str
    reply_to_message_id: int | None
    mentioned_user_ids: list[int]
    timestamp: datetime.datetime
    image_url: str | None = None
    pdf_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        """プロンプト作成に用いる要素のみを辞書形式で出力します。

        Returns:
            人物と返信先をDiscord IDで識別できる辞書

        """
        return {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "reply_to_message_id": self.reply_to_message_id,
            "mentioned_user_ids": self.mentioned_user_ids,
            "timestamp": self.timestamp.astimezone(LOCAL_TIMEZONE).isoformat(),
        }


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
        image_url = None
        pdf_url = None

        # 2. reply_to に関する特殊処理

        if message.message_snapshots:  # メッセージが転送である場合
            content = f"{author_name}がメッセージを転送: 「{message.message_snapshots[0].content}」"

        if message.type == discord.MessageType.reply and message.reference and message.reference.message_id:
            reply_to_message_id = message.reference.message_id
        elif message.type == discord.MessageType.reply:
            logger.warning(
                "Message is a reply but referenced message not found (ref_id=%s, channel_id=%s, guild_id=%s)",
                message.reference.message_id if message.reference else None,
                message.channel.id,
                message.guild.id if message.guild else None,
            )

        # 3. 添付ファイルに関する特殊処理

        for attachment in message.attachments:
            if attachment.content_type in ("image/jpeg", "image/png"):
                # OpenAI supports PNG (.png), JPEG (.jpeg, .jpg), WEBP (.webp), and Non-animated GIF (.gif).
                # Files with uncommon extensions (e.g., .jfif) may cause errors.
                # see https://platform.openai.com/docs/guides/images-vision
                image_url = attachment.url

            if attachment.content_type == "application/pdf":
                pdf_url = attachment.url

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
                image_url=image_url,
                pdf_url=pdf_url,
            )
        )

        # 5. メモリ内のメッセージを日時順にソート
        self.memory.sort(key=lambda m: m.timestamp)

        logger.debug("Current messages in memory: %s", self.memory)

    def to_json(self) -> str:
        """短期記憶内のメッセージをプロンプトに用いるJSON形式の文字列に変換します。

        Returns:
            人物と返信先をDiscord IDで識別できるJSON表現

        """
        return json.dumps([m.to_dict() for m in self.memory], ensure_ascii=False, indent=2)

    def forget(self, maximum_token: int = 5000) -> None:
        """メモリ内のメッセージを古い順に削除して、トークン数を制限以下に保ちます。

        Args:
            maximum_token: 保持される最大トークン数 (デフォルト: 5000)

        """
        while self.memory:
            text = json.dumps(
                [m.to_dict() for m in self.memory],
                ensure_ascii=False,
            )
            token_count = len(self.encoding.encode(text))

            if token_count <= maximum_token:
                break

            self.memory.pop(0)

        logger.debug(
            "Current messages in memory: %s tokens",
            len(
                self.encoding.encode(
                    json.dumps(
                        [m.to_dict() for m in self.memory],
                        ensure_ascii=False,
                    )
                )
            ),
        )

        logger.debug("Current memory: %s", self.memory)

    def contains_message(self, message_id: int) -> bool:
        """指定されたDiscordメッセージが現在の短期記憶に含まれるか確認します。"""
        return any(message.message_id == message_id for message in self.memory)

    def get_author_id(self, message_id: int) -> int | None:
        """指定されたDiscordメッセージの発言者IDを取得します。"""
        for message in self.memory:
            if message.message_id == message_id:
                return message.author_id
        return None


class ResponseAction(StrEnum):
    """LLMが選択できるDiscord上の応答方法。"""

    SILENCE = "silence"
    REACTION = "reaction"
    REPLY = "reply"
    MESSAGE = "message"


class LLMMessage(BaseModel):
    """OpenAI APIによって生成されるメッセージのデータモデル。

    Attributes:
        content: メッセージの内容
        action: Discord上で実行する応答方法
        reply_to_message_id: 返信先のDiscordメッセージID。通常投稿の場合はNone
        reaction_emoji: リアクションに使用するUnicode絵文字

    """

    action: ResponseAction
    content: str = ""
    reply_to_message_id: int | None = None
    reaction_emoji: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        """選択した行動の実行に必要なフィールドが揃っていることを保証します。"""
        if self.action is ResponseAction.REPLY and self.reply_to_message_id is None:
            msg = "reply action requires reply_to_message_id"
            raise ValueError(msg)
        if self.action is ResponseAction.REACTION and (self.reply_to_message_id is None or self.reaction_emoji is None):
            msg = "reaction action requires reply_to_message_id and reaction_emoji"
            raise ValueError(msg)
        if self.action in (ResponseAction.REPLY, ResponseAction.MESSAGE) and not self.content.strip():
            msg = "text response action requires content"
            raise ValueError(msg)
        return self

    def to_json(self, bot_name: str) -> str:
        """メッセージをJSON形式の文字列に変換します。

        Args:
            bot_name: botの名前

        Returns:
            メッセージのJSON表現

        """
        return json.dumps(
            {
                "author_name": bot_name,
                "action": self.action.value,
                "content": self.content,
                "reply_to_message_id": self.reply_to_message_id,
                "reaction_emoji": self.reaction_emoji,
            },
            ensure_ascii=False,
            indent=2,
        )


class DraftGenerator:
    """回答のドラフト生成を担当するクラス。OpenAI APIを使用して回答のドラフトを生成します。"""

    def __init__(self, client: AsyncOpenAI, bot_name: str) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント
            bot_name: botの名前

        """
        self.client = client
        self.bot_name = bot_name

    async def draft(self, short_term_memory: ShortTermMemory, channel_role: ChannelRole) -> LLMMessage:
        """メッセージのドラフト回答を生成します。

        Args:
            short_term_memory: メッセージ履歴
            channel_role: 対象チャンネルでのChatbotの役割

        Returns:
            生成されたドラフト回答を含むLLMMessageオブジェクト

        """
        # 履歴内の画像とPDFは、入力サイズを制御する方針が決まるまで直接の返信元だけを対象とする。

        llm_input: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": short_term_memory.to_json(),
                    }
                ],
            }
        ]

        if short_term_memory.memory[-1].image_url:
            llm_input[0]["content"].append({"type": "input_image", "image_url": short_term_memory.memory[-1].image_url})

        if short_term_memory.memory[-1].pdf_url:
            llm_input[0]["content"].append({"type": "input_file", "file_url": short_term_memory.memory[-1].pdf_url})

        api_response = await self.client.responses.parse(
            input=llm_input,  # type: ignore
            instructions=draft_generator_prompt.DRAFT_INSTRUCTIONS.format(
                bot_name=self.bot_name,
                channel_role=channel_role.value,
            ),
            model=DRAFT_GENERATOR_MODEL,
            reasoning={"effort": "medium"},
            tools=[
                {
                    "type": "web_search",
                    "user_location": {"type": "approximate", "country": "JP"},
                },
                {
                    "type": "code_interpreter",
                    "container": {"type": "auto"},
                },
            ],
            text_format=LLMMessage,
        )

        if api_response.output_parsed is None:
            logger.warning("Failed to parse LLM response into LLMMessage")
            return LLMMessage(action=ResponseAction.SILENCE)

        return api_response.output_parsed


class ResponseStyler:
    """回答の形式面を調整するクラス。ドラフトを整形して最終回答を生成します。"""

    def __init__(self, client: AsyncOpenAI, bot_name: str) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント
            bot_name: botの名前

        """
        self.client = client
        self.bot_name = bot_name

    async def style(
        self,
        short_term_memory: ShortTermMemory,
        original_draft: LLMMessage,
        channel_role: ChannelRole,
    ) -> LLMMessage:
        """ドラフトをスタイリングして最終回答を生成します。

        Args:
            short_term_memory: メッセージ履歴
            original_draft: DraftGeneratorが生成した原案
            channel_role: 対象チャンネルでのChatbotの役割

        Returns:
            スタイリングされた回答を含むLLMMessageオブジェクト

        """
        api_response = await self.client.responses.parse(
            instructions=response_styler_prompt.STYLE_INSTRUCTIONS.format(
                bot_name=self.bot_name,
                channel_role=channel_role.value,
            ),
            input=response_styler_prompt.STYLE_INPUT.format(
                short_term_memory=short_term_memory.to_json(),
                draft=original_draft.to_json(bot_name=self.bot_name),
            ),
            model=STYLER_MODEL,
            reasoning={"effort": "low"},
            text_format=LLMMessage,
        )

        if api_response.output_parsed is None:
            logger.warning("Failed to parse LLM response into LLMMessage")
            return original_draft

        return LLMMessage(
            action=original_draft.action,
            content=api_response.output_parsed.content,
            reply_to_message_id=original_draft.reply_to_message_id,
            reaction_emoji=original_draft.reaction_emoji,
        )


class ResponsePipeline:
    """ドラフト生成とスタイリングを一連の流れで実行するクラス。"""

    def __init__(self, client: AsyncOpenAI, bot_name: str) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント
            bot_name: botの名前

        """
        self.draft_generator = DraftGenerator(client, bot_name)
        self.response_styler = ResponseStyler(client, bot_name)
        self.short_term_memory = ShortTermMemory()
        self.bot_name = bot_name

    async def add_message_to_memory(self, message: Message) -> None:
        """Discordのメッセージを短期記憶に追加します。

        Args:
            message: 追加するDiscordメッセージ

        """
        await self.short_term_memory.append(message)
        self.short_term_memory.forget()

    async def generate_response(self, channel_role: ChannelRole) -> LLMMessage:
        """短期記憶から最終回答を生成します。

        Args:
            channel_role: 対象チャンネルでのChatbotの役割

        Returns:
            スタイリングされた最終回答を含むLLMMessageオブジェクト

        """
        draft = await self.draft_generator.draft(self.short_term_memory, channel_role)
        if draft.action in (ResponseAction.SILENCE, ResponseAction.REACTION):
            return draft
        return await self.response_styler.style(self.short_term_memory, draft, channel_role)
