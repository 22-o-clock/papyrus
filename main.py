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


class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        await load_all_cogs(self)
        my_server = await self.fetch_guild(int(os.environ["SERVER_ID"]))
        self.tree.copy_global_to(guild=my_server)
        await self.tree.sync(guild=my_server)


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
