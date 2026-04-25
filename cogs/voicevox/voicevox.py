from logging import getLogger
import os

from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import VoiceVoxDatabase

logger = getLogger(__name__)


class Voicevox(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot: commands.Bot = bot
        self.character_for_member = dict()
        self.voicevox_url = os.environ["VOICEVOX_URL"]
        self.db = VoiceVoxDatabase(session_factory)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.character_for_member = await self.db.get_speakers()

async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(Voicevox(bot, session_factory))
