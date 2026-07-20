import json
import os
from logging import config, getLogger
from pathlib import Path

import dotenv
from discord import Intents
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cogs import (
    admin,
    agree,
    api_usage,
    audit,
    chatbot,
    hwh,
    monitor,
    moving,
    remind,
    speak,
    spotify_embed,
    talkdata,
    voice,
    voicevox,
)
from cogs.chatbot.repositories.schema import CHATBOT_DATABASE_SCHEMA, create_chatbot_tables
from core.db import create_session_factory, create_tables, dispose_engine, init_engine
from core.debug_cogs import load_debug_cogs
from core.runtime_environment import configure_runtime_environment, get_runtime_environment


async def load_all_cogs(
    bot: commands.Bot,
    session_factory: async_sessionmaker,
    chatbot_session_factory: async_sessionmaker,
) -> None:
    await admin.setup(bot)
    await agree.setup(bot)
    await audit.setup(bot)
    await api_usage.setup(bot, chatbot_session_factory)
    await chatbot.setup(bot, chatbot_session_factory)
    await hwh.setup(bot)
    await monitor.setup(bot)
    await moving.setup(bot)
    await remind.setup(bot, session_factory)
    await speak.setup(bot)
    await spotify_embed.setup(bot)
    await talkdata.setup(bot, session_factory)
    await voice.setup(bot, session_factory)
    await voicevox.setup(bot, session_factory)


class MyBot(commands.Bot):
    _chatbot_engine: AsyncEngine | None = None

    async def setup_hook(self) -> None:
        session_factory = init_engine()
        chatbot_engine, chatbot_session_factory = create_session_factory(
            os.environ["CHATBOT_SUPABASE_CONNECTION_STRING"],
            search_path=f"{CHATBOT_DATABASE_SCHEMA},extensions,public",
        )
        self._chatbot_engine = chatbot_engine
        if os.getenv("DEBUG", "").lower() == "true":
            await load_debug_cogs(self, session_factory, chatbot_session_factory)
        else:
            await load_all_cogs(self, session_factory, chatbot_session_factory)
        runtime = get_runtime_environment()
        if runtime.is_production:
            await create_tables()
            await create_chatbot_tables(chatbot_engine)
        else:
            getLogger(__name__).info("Skipped shared database schema updates in debug environment")
        my_server = await self.fetch_guild(int(os.environ["SERVER_ID"]))
        self.tree.copy_global_to(guild=my_server)
        await self.tree.sync(guild=my_server)

    async def close(self) -> None:
        await dispose_engine()
        if self._chatbot_engine is not None:
            await self._chatbot_engine.dispose()
            self._chatbot_engine = None
        await super().close()


def main() -> None:
    dotenv.load_dotenv()
    configure_runtime_environment()

    with Path("./log/logging_conf.json").open("r", encoding="utf-8") as f:
        config.dictConfig(json.load(f))

    cogs_logger = getLogger("cogs")
    cogs_logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

    bot = MyBot(command_prefix="!?", intents=Intents.all())
    bot.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
