import re
from logging import getLogger

from discord import Interaction, Message, app_commands
from discord.ext import commands

logger = getLogger(__name__)


class Agree(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.agree_menu = app_commands.ContextMenu(name="agree", callback=self.agree)
        self.disagree_menu = app_commands.ContextMenu(name="disagree", callback=self.disagree)
        bot.tree.add_command(self.agree_menu)
        bot.tree.add_command(self.disagree_menu)

    async def agree(self, interaction: Interaction, message: Message) -> None:
        """
        指定したメッセージをbotに復唱させます。その際、前の発言者がn番目に発言した場合はメッセージの末尾にn+1を付け加えます。
        """

        if message_structure := re.findall(r"(.*?)(\d+)$", message.content, re.DOTALL):
            text: str = message_structure[0][0]
            num: int = int(message_structure[0][1])
            response = f"{text}[{num + 1}](<{message.jump_url}>)"
        elif message_structure := re.findall(r"(.*)\[(\d+)\]\(<.*>\)$", message.content, re.DOTALL):
            text: str = message_structure[0][0]
            num: int = int(message_structure[0][1])
            response = f"{text}[{num + 1}](<{message.jump_url}>)"
        else:
            response = message.content + f"[2](<{message.jump_url}>)"

        await interaction.response.send_message(response)

    async def disagree(self, interaction: Interaction, message: Message):
        """
        メッセージの末尾に「↑そんなことはないですね」を追加して返す。
        """
        await interaction.response.send_message("> " + message.content + "\n↑そんなことはないですね")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Agree(bot))
    logger.debug(f"{__name__} is added to the bot.")
