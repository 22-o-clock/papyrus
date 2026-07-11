import json
import os
from logging import config, getLogger
from pathlib import Path

import dotenv
from discord import Intents
from discord.ext import commands
from sqlalchemy.ext.asyncio import async_sessionmaker

from cogs import admin, agree, chatbot, remind, speak, voicevox
from core.db import dispose_engine, init_engine


async def load_all_cogs(bot: commands.Bot, session_factory: async_sessionmaker) -> None:
    await admin.setup(bot)
    await agree.setup(bot)
    await chatbot.setup(bot, session_factory)
    await remind.setup(bot, session_factory)
    await speak.setup(bot)
    await voicevox.setup(bot, session_factory)


class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        session_factory = init_engine()
        await load_all_cogs(self, session_factory)
        my_server = await self.fetch_guild(int(os.environ["SERVER_ID"]))
        self.tree.copy_global_to(guild=my_server)
        await self.tree.sync(guild=my_server)

    async def close(self) -> None:
        await dispose_engine()
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
