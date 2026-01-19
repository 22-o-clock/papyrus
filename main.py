import asyncio
import json
import os
from logging import config, getLogger
from pathlib import Path

import dotenv
from discord import Intents
from discord.ext import commands

from cogs import chatbot


async def load_all_cogs(bot: commands.Bot) -> None:
    await chatbot.setup(bot)


def main() -> None:
    dotenv.load_dotenv()

    with Path("./log/logging_conf.json").open("r", encoding="utf-8") as f:
        config.dictConfig(json.load(f))

    cogs_logger = getLogger("cogs")
    cogs_logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

    bot = commands.Bot(command_prefix="!?", intents=Intents.all())
    asyncio.run(load_all_cogs(bot))
    bot.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
