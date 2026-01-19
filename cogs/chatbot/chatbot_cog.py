import os
from logging import getLogger

from discord import Message, TextChannel, app_commands
from discord.ext import commands
from openai import AsyncOpenAI

from .responses_api import fetch_chatgpt_output_text

logger = getLogger(__name__)

OPENAI_MODEL = os.getenv("RESPONSES_API_MODEL", "gpt-4.1")


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = AsyncOpenAI()
        self.latest_response: str | None = None
        self.is_responding = False
        self.target_channel: TextChannel | None = None

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            return

        for user in message.mentions:
            if self.bot.user is not None and user.id == self.bot.user.id:
                break
        else:
            return

        response_text, self.latest_response = await fetch_chatgpt_output_text(
            self.client, message, previous_response_id=self.latest_response
        )

        await message.reply(response_text)

    @app_commands.command()
    async def summon(self, ctx) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatBot(bot))
