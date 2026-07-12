import os
import re
from logging import getLogger
from secrets import choice

from discord import Interaction, Member, app_commands
from discord.ext import commands
from jaconv import hira2kata
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import ArknightsVoiceDatabase

logger = getLogger(__name__)


class ArknightsVoice(commands.Cog):
    """アークナイツのキャラクターボイスを検索・送信するCog。"""

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.db = ArknightsVoiceDatabase(session_factory)
        self.character_names: list[tuple[str, str, str | None]] = []
        self.name_match: re.Pattern[str] | None = None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.character_names = await self.db.get_character_names()

        guild = await self.bot.fetch_guild(int(os.environ["SERVER_ID"]))
        display_names = [member.display_name async for member in guild.fetch_members()]
        mention_patterns = [re.escape(f"@{display_name}") for display_name in display_names]
        self.name_match = re.compile("|".join([*mention_patterns, r"<@!?[0-9]+>"]))
        logger.info("Arknights voice cog is ready.")

    async def interaction_check(self, interaction: Interaction) -> bool:
        """初期化前のコマンド実行を利用者向けメッセージで中断する。"""
        if self.name_match is not None:
            return True

        await interaction.response.send_message("ボットは起動準備中です。もう少しお待ちください 💦", ephemeral=True)
        return False

    @app_commands.command(
        description="ランダムにアークナイツのキャラクターの台詞を返します。キャラクターを指定することもできます。"
    )
    @app_commands.describe(name="キャラクター名")
    async def amiya(self, interaction: Interaction, name: str | None = None) -> None:
        ch_name = self._resolve_character_name(name) if name else None
        if name is not None and ch_name is None:
            await interaction.response.send_message("指定したキャラクターは見つかりませんでした 💦", ephemeral=True)
            return

        phrases = await self.db.get_phrases(ch_name)
        if not phrases:
            await interaction.response.send_message("指定したキャラクターのボイスはありません 💦", ephemeral=True)
            return

        await interaction.response.send_message(choice(phrases)[1])

    @amiya.autocomplete("name")
    async def amiya_autocomplete(self, _: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._autocomplete_character_name(current)

    def _autocomplete_character_name(self, current: str) -> list[app_commands.Choice[str]]:
        """入力中の文字列に前方一致するキャラクター候補を返す。"""
        normalized_current = hira2kata(current.lstrip().lower())
        return [
            app_commands.Choice(name=display_name, value=display_name)
            for display_name in self._display_names()
            if display_name.lower().startswith(normalized_current)
        ][:25]

    @app_commands.command(description="アークナイツの「ドクター」を含む台詞をメンションに置き換えて返します。")
    @app_commands.describe(name="キャラクター名")
    async def doctor(self, interaction: Interaction, name: str | None = None) -> None:
        ch_name = self._resolve_character_name(name) if name else None
        if name is not None and ch_name is None:
            await interaction.response.send_message("指定したキャラクターは見つかりませんでした 💦", ephemeral=True)
            return

        phrases = await self.db.find_phrases_containing("ドクター", ch_name)
        if not phrases:
            await interaction.response.send_message(
                "指定したキャラクターに「ドクター」を含むボイスはありません 💦", ephemeral=True
            )
            return

        await interaction.response.send_message(choice(phrases).replace("ドクター", interaction.user.mention))

    @doctor.autocomplete("name")
    async def doctor_autocomplete(self, _: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._autocomplete_character_name(current)

    @app_commands.command(description="休むなドクターの台詞を送信します。")
    async def doctor_yasumuna(self, interaction: Interaction, user: Member | None = None) -> None:
        target = user.mention if user is not None else interaction.user.mention
        await interaction.response.send_message(f"{target}、終わってない仕事がたくさんありますから、まだ休んじゃダメですよ。")

    @app_commands.command(description="指定した台詞のキャラクターとボイス種別を検索します。")
    async def whose(self, interaction: Interaction, phrase: str) -> None:
        if self.name_match is None:
            await interaction.response.send_message("ボットは起動準備中です。もう少しお待ちください 💦", ephemeral=True)
            return

        matches = await self.db.find_phrase(self.name_match.sub("ドクター", phrase))
        if len(matches) == 1:
            ch_name, category = matches[0]
            await interaction.response.send_message(
                f"☑️ 当該ボイスは、アークナイツ - {self._display_name(ch_name)} の{category}ボイスです。"
            )
        elif matches:
            await interaction.response.send_message("該当キャラが複数存在するため特定できませんでした… 💦")
        else:
            await interaction.response.send_message("キャッシュ内に当該ボイスが見つかりませんでした…💦")

    def _display_names(self) -> list[str]:
        return [self._display_name(ch_name) for ch_name, _, _ in self.character_names]

    def _display_name(self, ch_name: str) -> str:
        for stored_ch_name, en_name, jp_name in self.character_names:
            if stored_ch_name == ch_name:
                return jp_name or en_name
        return ch_name

    def _resolve_character_name(self, name: str) -> str | None:
        for ch_name, en_name, jp_name in self.character_names:
            if name in (ch_name, en_name, jp_name, self._display_name(ch_name)):
                return ch_name
        return None


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(ArknightsVoice(bot, session_factory))
