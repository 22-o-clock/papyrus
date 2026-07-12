import traceback
from logging import getLogger

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from core.exception.exception import BotError, HandledError, MissingRequiredRoleError

logger = getLogger(__name__)


class ErrorHandler(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.tree.error(self.on_error)

    async def on_error(self, interaction: Interaction, error: app_commands.AppCommandError) -> None:
        log_text = f"on_app_command_error catches {type(error).__name__}"

        if isinstance(error, app_commands.CommandInvokeError):
            origin = error.original
            log_text += f", original error: {type(origin).__name__}"

            if isinstance(origin, MissingRequiredRoleError):
                logger.error(log_text)
                await self._respond(interaction, "このコマンドを実行するために必要な権限がありません... 💦", ephemeral=True)
                return

            if isinstance(origin, HandledError):
                logger.error(log_text)
                return

            if isinstance(origin, BotError):
                logger.error("%s, message: %s", log_text, origin)
                await self._respond(interaction, f"{type(origin).__name__}: {origin}", ephemeral=True)
                return

            logger.error("%s\n%s", log_text, "".join(traceback.format_exception(origin)))
            try:
                await self._respond(interaction, f"{type(origin).__name__}: {origin}", ephemeral=True)
            except discord.HTTPException:
                logger.exception("Failed to send error message on Discord.")

        else:
            logger.error("%s\n%s", log_text, "".join(traceback.format_exception(error)))

    async def _respond(self, interaction: Interaction, content: str, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ErrorHandler(bot))
