import json
import os
from logging import config, getLogger

import dotenv
from discord import Intents
from discord.ext import commands

from cogs import chatbot


def load_all_cogs(bot: commands.Bot):
    chatbot.setup(bot)


def main():
    dotenv.load_dotenv()

    with open("./log/logging_conf.json", "r", encoding="utf-8") as f:
        config.dictConfig(json.load(f))

    cogs_logger = getLogger("cogs")
    cogs_logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

    bot = commands.Bot(intents=Intents.all())
    load_all_cogs(bot)
    bot.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
