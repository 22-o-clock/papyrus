import json
import os
from logging import config, getLogger
from pathlib import Path

import dotenv
from discord import Intents
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cogs import admin, agree, chatbot, remind, speak, voicevox
from cogs.chatbot.database import CHATBOT_DATABASE_SCHEMA, ChatbotBase
from core.db import create_session_factory, create_tables, create_tables_for, dispose_engine, init_engine


async def load_all_cogs(
    bot: commands.Bot,
    session_factory: async_sessionmaker,
    chatbot_session_factory: async_sessionmaker,
) -> None:
    await admin.setup(bot)
    await agree.setup(bot)
    await chatbot.setup(bot, chatbot_session_factory)
    await remind.setup(bot, session_factory)
    await speak.setup(bot)
    await voicevox.setup(bot, session_factory)


class MyBot(commands.Bot):
    _chatbot_engine: AsyncEngine | None = None

    async def setup_hook(self) -> None:
        session_factory = init_engine()
        chatbot_engine, chatbot_session_factory = create_session_factory(os.environ["CHATBOT_SUPABASE_CONNECTION_STRING"])
        self._chatbot_engine = chatbot_engine
        await load_all_cogs(self, session_factory, chatbot_session_factory)
        await create_tables()
        await create_tables_for(chatbot_engine, ChatbotBase.metadata, schema=CHATBOT_DATABASE_SCHEMA)
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

    with Path("./log/logging_conf.json").open("r", encoding="utf-8") as f:
        config.dictConfig(json.load(f))

    cogs_logger = getLogger("cogs")
    cogs_logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

    bot = MyBot(command_prefix="!?", intents=Intents.all())
    bot.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
