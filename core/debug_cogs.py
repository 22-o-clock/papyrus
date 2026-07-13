from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def load_debug_cogs(
    _bot: commands.Bot,
    _session_factory: async_sessionmaker[AsyncSession],
    _chatbot_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """開発対象のCogだけを起動します。"""
