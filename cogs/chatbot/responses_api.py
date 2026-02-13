import datetime
import json
from dataclasses import dataclass
from logging import getLogger
from typing import Any

import dateutil
import discord
import tiktoken
from discord import Message
from openai import AsyncOpenAI
from pydantic import BaseModel

from .prompt import draft_generator_prompt, response_styler_prompt

logger = getLogger(__name__)

OPENAI_MODEL = "gpt-5.2"
LOCAL_TIMEZONE = dateutil.tz.gettz("Asia/Tokyo")


@dataclass
class MessageInMemory:
    """短期記憶内に保存されるメッセージを表すデータクラス。

    Attributes:
        message_id: メッセージのID
        author_name: メッセージの送信者の名前
        content: メッセージの内容
        reply_to: 返信先のメッセージの送信者名
        timestamp: メッセージが作成された日時

    """

    message_id: int
    author_name: str
    content: str
    reply_to: str
    timestamp: datetime.datetime

    def to_dict(self) -> dict[str, str]:
        """プロンプト作成に用いる要素のみを辞書形式で出力します。

        Returns:
            author_name、content、reply_toを含む辞書

        """
        return {
            "author_name": self.author_name,
            "content": self.content,
            "reply_to": self.reply_to,
        }


class ShortTermMemory:
    """短期記憶を管理するクラス。メッセージの履歴をトークン数の制限内で保持します。"""

    def __init__(self, model: str = "gpt-5-") -> None:
        """短期メモリを初期化します。

        Args:
            model: トークンカウント用のtiktokenのモデル名（デフォルト: "gpt-5-"）

        """
        self.memory: list[MessageInMemory] = []
        self.encoding = tiktoken.encoding_for_model(model)

    async def append(self, message: Message) -> None:
        """メッセージを短期記憶に追加します。

        Args:
            message: 追加するDiscordメッセージ

        """
        reply_to = "All"

        if message.reference and message.reference.message_id:
            try:
                target_message = await message.channel.fetch_message(message.reference.message_id)
                reply_to = target_message.author.display_name
            except discord.errors.NotFound:
                if isinstance(message.channel, discord.Thread) and isinstance(message.channel.parent, discord.TextChannel):
                    target_message = await message.channel.parent.fetch_message(message.reference.message_id)
                    reply_to = target_message.author.display_name
                else:
                    logger.warning(
                        "Referenced message not found (ref_id=%s, channel_id=%s, guild_id=%s)",
                        message.reference.message_id,
                        message.channel.id,
                        message.guild.id if message.guild else None,
                    )

        self.memory.append(
            MessageInMemory(
                message_id=message.id,
                author_name=message.author.display_name,
                content=message.clean_content,
                reply_to=reply_to,
                timestamp=message.created_at,
            )
        )

    def to_json(self) -> str:
        """短期記憶内のメッセージをプロンプトに用いるJSON形式の文字列に変換します。

        Returns:
            メモリ内のメッセージのJSON表現

        """
        return json.dumps([m.to_dict() for m in self.memory], ensure_ascii=False, indent=2)

    def forget(self, maximum_token: int = 5000) -> None:
        """メモリ内のメッセージを古い順に削除して、トークン数を制限以下に保ちます。

        Args:
            maximum_token: 保持される最大トークン数（デフォルト: 5000）

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

        logger.info(
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


def convert_message_to_chatgpt_input(message: Message) -> list[dict[str, Any]]:
    """Discordメッセージを ChatGPT API入力形式に変換します。

    Args:
        message: 変換するDiscordメッセージ

    Returns:
        ChatGPT API形式のメッセージリスト

    """
    chatgpt_input: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": message.clean_content,
                }
            ],
        }
    ]

    for attachment in message.attachments:
        if attachment.content_type in ("image/jpeg", "image/png"):
            # OpenAI supports PNG (.png), JPEG (.jpeg, .jpg), WEBP (.webp), and Non-animated GIF (.gif).
            # Files with uncommon extensions (e.g., .jfif) may cause errors.
            # see https://platform.openai.com/docs/guides/images-vision

            chatgpt_input[0]["content"].append({"type": "input_image", "image_url": attachment.url})

        if attachment.content_type == "application/pdf":
            chatgpt_input[0]["content"].append({"type": "input_file", "file_url": attachment.url})

    return chatgpt_input


class LLMMessage(BaseModel):
    """OpenAI APIによって生成されるメッセージのデータモデル。

    Attributes:
        content: メッセージの内容
        reply_to: 返信先のメッセージの送信者名

    """

    content: str
    reply_to: str

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
                "content": self.content,
                "reply_to": self.reply_to,
            },
            ensure_ascii=False,
            indent=2,
        )


class DraftGenerator:
    """回答のドラフト生成を担当するクラス。OpenAI APIを使用して回答のドラフトを生成します。"""

    def __init__(self, client: AsyncOpenAI) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント

        """
        self.client = client

    async def draft(self, bot_name: str, short_term_memory: ShortTermMemory) -> LLMMessage:
        """メッセージのドラフト回答を生成します。

        Args:
            bot_name: botの名前
            short_term_memory: メッセージ履歴

        Returns:
            生成されたドラフト回答を含むLLMMessageオブジェクト

        """
        api_response = await self.client.responses.parse(
            input=short_term_memory.to_json(),
            instructions=draft_generator_prompt.DRAFT_INSTRUCTIONS.format(bot_name=bot_name),
            model=OPENAI_MODEL,
            reasoning={"effort": "medium"},
            tools=[
                {
                    "type": "web_search",
                    "user_location": {"type": "approximate", "country": "JP"},
                }
            ],
            text_format=LLMMessage,
        )

        if api_response.output_parsed is None:
            logger.warning("Failed to parse LLM response into LLMMessage")
            return LLMMessage(content="", reply_to="All")

        return api_response.output_parsed


class ResponseStyler:
    """回答の形式面を調整するクラス。ドラフトを整形して最終回答を生成します。"""

    def __init__(self, client: AsyncOpenAI) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント

        """
        self.client = client

    async def style(self, bot_name: str, short_term_memory: ShortTermMemory, original_draft: LLMMessage) -> LLMMessage:
        """ドラフトをスタイリングして最終回答を生成します。

        Args:
            bot_name: botの名前
            short_term_memory: メッセージ履歴
            original_draft: DraftGeneratorが生成した原案

        Returns:
            スタイリングされた回答を含むLLMMessageオブジェクト

        """
        api_response = await self.client.responses.parse(
            instructions=response_styler_prompt.STYLE_INSTRUCTIONS.format(bot_name=bot_name),
            input=response_styler_prompt.STYLE_INPUT.format(
                short_term_memory=short_term_memory.to_json(),
                draft=original_draft.to_json(bot_name=bot_name),
            ),
            model=OPENAI_MODEL,
            text_format=LLMMessage,
        )

        if api_response.output_parsed is None:
            logger.warning("Failed to parse LLM response into LLMMessage")
            return LLMMessage(content="", reply_to="All")

        return api_response.output_parsed
