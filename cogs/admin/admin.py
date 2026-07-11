import traceback
from logging import getLogger

from discord import Interaction, app_commands
from discord.ext import commands

from core.exception.exception import BotException, HandledError, MissingRequiredRole

logger = getLogger(__name__)


class ErrorHandler(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.tree.on_error = self.on_error

    async def on_error(self, interaction: Interaction, error: app_commands.AppCommandError) -> None:
        log_text = f"on_app_command_error catches {type(error).__name__}"

        if isinstance(error, app_commands.CommandInvokeError):
            origin = error.original
            log_text += f", original error: {type(origin).__name__}"

            if isinstance(origin, MissingRequiredRole):
                logger.error(log_text)
                await self._respond(interaction, "このコマンドを実行するために必要な権限がありません... 💦", ephemeral=True)
                return

            if isinstance(origin, HandledError):
                logger.error(log_text)
                return

            if isinstance(origin, BotException):
                logger.error(log_text + f", message: {origin}")
                await self._respond(interaction, f"{type(origin).__name__}: {origin}", ephemeral=True)
                return

            logger.error(log_text + f"\n{''.join(traceback.format_exception(origin))}")
            try:
                await self._respond(interaction, f"{type(origin).__name__}: {origin}", ephemeral=True)
            except Exception:
                logger.error("Failed to send error message on Discord.")

        else:
            logger.error(log_text + f"\n{''.join(traceback.format_exception(error))}")

    async def _respond(self, interaction: Interaction, content: str, **kwargs) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ErrorHandler(bot))
