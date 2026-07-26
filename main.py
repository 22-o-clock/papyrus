from __future__ import annotations

import json
import os
from logging import config, getLogger
from pathlib import Path
from typing import TYPE_CHECKING

import dotenv
from discord import Intents
from discord.ext import commands

from cogs import (
    admin,
    agree,
    api_usage,
    audit,
    chatbot,
    cynicism,
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
from core.runtime_environment import AVAILABLE_COG_NAMES, configure_runtime_environment, get_runtime_environment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


async def load_cogs(
    bot: commands.Bot,
    session_factory: async_sessionmaker,
    chatbot_session_factory: async_sessionmaker,
    enabled_cogs: tuple[str, ...],
) -> None:
    """検証済みのCogだけを既定順にロードします。"""
    loaders: dict[str, Callable[[], Awaitable[None]]] = {
        "admin": lambda: admin.setup(bot),
        "agree": lambda: agree.setup(bot),
        "audit": lambda: audit.setup(bot),
        "api_usage": lambda: api_usage.setup(bot, chatbot_session_factory),
        "chatbot": lambda: chatbot.setup(bot, chatbot_session_factory),
        "cynicism": lambda: cynicism.setup(bot, session_factory),
        "hwh": lambda: hwh.setup(bot),
        "monitor": lambda: monitor.setup(bot),
        "moving": lambda: moving.setup(bot),
        "remind": lambda: remind.setup(bot, session_factory),
        "speak": lambda: speak.setup(bot),
        "spotify_embed": lambda: spotify_embed.setup(bot),
        "talkdata": lambda: talkdata.setup(bot, session_factory),
        "voice": lambda: voice.setup(bot, session_factory),
        "voicevox": lambda: voicevox.setup(bot, session_factory),
    }
    if tuple(loaders) != AVAILABLE_COG_NAMES:
        message = "Cog loader registry does not match AVAILABLE_COG_NAMES"
        raise RuntimeError(message)

    for cog_name in enabled_cogs:
        await loaders[cog_name]()


class MyBot(commands.Bot):
    _chatbot_engine: AsyncEngine | None = None

    async def setup_hook(self) -> None:
        session_factory = init_engine()
        chatbot_engine, chatbot_session_factory = create_session_factory(
            os.environ["CHATBOT_SUPABASE_CONNECTION_STRING"],
            search_path=f"{CHATBOT_DATABASE_SCHEMA},extensions,public",
        )
        self._chatbot_engine = chatbot_engine
        runtime = get_runtime_environment()
        await load_cogs(self, session_factory, chatbot_session_factory, runtime.enabled_cogs)
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
