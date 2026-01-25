from logging import getLogger
from typing import Any

from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI

from .responses_api import DraftGenerator, ResponseStyler

logger = getLogger(__name__)


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = AsyncOpenAI()
        self.draft_generator = DraftGenerator(self.client)
        self.response_styler = ResponseStyler(self.client)
        self.target_channel: int = 0
        self.short_term_memory: list[Message] = []

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.channel.id == self.target_channel:
            self.add_message_to_short_term_memory(message)

        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return

        is_reply_required = await self.is_reply_required(message)

        if is_reply_required:  # TODO @se-anthyme: ロック処理を追加
            response_text = await self.generate_text_response()
            await message.reply(response_text)

    def add_message_to_short_term_memory(self, message: Message) -> None:
        self.short_term_memory.append(message)

    async def is_reply_required(self, message: Message) -> bool:
        # TODO @se-anthyme: 判定ロジックを追加
        return True

    async def generate_text_response(self) -> str:
        if self.bot.user is None:
            raise TypeError

        draft = await self.draft_generator.draft(self.bot.user.id, self.short_term_memory)
        return await self.response_styler.style(draft)

    @commands.hybrid_command()
    async def summon(self, ctx: commands.Context[Any]) -> None:
        await ctx.send("Hello!")
        self.target_channel = ctx.channel.id


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatBot(bot))
