import datetime
import json
import os
from dataclasses import dataclass
from logging import getLogger

import discord
import tiktoken
from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI

from .responses_api import DraftGenerator, ResponseStyler

logger = getLogger(__name__)


@dataclass
class MessageInMemory:
    message_id: int
    author_name: str
    content: str
    reply_to: str
    timestamp: datetime.datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "author_name": self.author_name,
            "content": self.content,
            "reply_to": self.reply_to,
        }


class ShortTermMemory:
    def __init__(self, model: str = "gpt-5-") -> None:
        self.memory: list[MessageInMemory] = []
        self.encoding = tiktoken.encoding_for_model(model)

    async def append(self, message: Message) -> None:
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
        return json.dumps([m.to_dict() for m in self.memory], ensure_ascii=False, indent=2)

    def forget(self, maximum_token: int = 5000) -> None:
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
            "Current memory in cache: %s tokens",
            len(
                self.encoding.encode(
                    json.dumps(
                        [m.to_dict() for m in self.memory],
                        ensure_ascii=False,
                    )
                )
            ),
        )


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = AsyncOpenAI()
        self.draft_generator = DraftGenerator(self.client)
        self.response_styler = ResponseStyler(self.client)
        self.target_channel: int = 0
        self.short_term_memory = ShortTermMemory()

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.channel.id == int(os.environ["TARGET_CHANNEL"]):
            await self.short_term_memory.append(message)

        logger.info(self.short_term_memory.to_json())
        self.short_term_memory.forget()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatBot(bot))
