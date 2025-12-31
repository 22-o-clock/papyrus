import os
from logging import getLogger
from typing import Optional

from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI

from .responses_api import fetch_chatgpt_output_text

logger = getLogger(__name__)

OPENAI_MODEL = os.getenv("RESPONSES_API_MODEL", "gpt-4.1")


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = AsyncOpenAI()
        self.latest_response: Optional[str] = None

    @commands.Cog.listener()  # type: ignore
    async def on_message(self, message: Message):
        for atc in message.attachments:
            logger.debug(f"{atc.content_type=}")

        if message.author.bot:
            return

        for user in message.mentions:
            if user.id == self.bot.user.id:  # type: ignore
                break
        else:
            return

        response_text, self.latest_response = await fetch_chatgpt_output_text(
            self.client, message, previous_response_id=self.latest_response
        )

        await message.reply(response_text)


def setup(bot: commands.Bot):
    bot.add_cog(ChatBot(bot))
