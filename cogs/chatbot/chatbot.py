import discord
from discord import Message, app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.runtime_environment import get_runtime_environment

from .use_cases.alias_excel_management import AliasExcelManagementUseCases
from .use_cases.conversation import ConversationUseCases


class ChatBot(commands.Cog):
    """DiscordイベントとChatbotユースケースを接続するController。"""

    chatbot = app_commands.Group(name="chatbot", description="Chatbotの設定とデータを管理します。")

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.runtime_environment = get_runtime_environment()
        self.conversation_use_cases = ConversationUseCases(bot, session_factory)
        self.custom_profile_use_cases = self.conversation_use_cases.custom_profile_use_cases
        self.excel_management_use_cases = AliasExcelManagementUseCases(session_factory)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.conversation_use_cases.on_ready()

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        await self.conversation_use_cases.on_message(message)

    @commands.Cog.listener()
    async def on_exclude_from_long_term_memory(self, message: Message) -> None:
        await self.conversation_use_cases.exclude_from_long_term_memory(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message: Message) -> None:
        await self.conversation_use_cases.on_message_delete(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        await self.conversation_use_cases.on_message_edit(before, after)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.conversation_use_cases.on_raw_reaction_change(payload.message_id, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.conversation_use_cases.on_raw_reaction_change(payload.message_id, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        await self.conversation_use_cases.on_raw_reaction_change(payload.message_id, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        await self.conversation_use_cases.on_raw_reaction_change(payload.message_id, payload.channel_id)

    @chatbot.command(name="profile_save", description="Chatbotのカスタムプロファイルを保存します")
    @app_commands.describe(
        profile_name="optionで指定する名前",
        instructions="基本指示へ追加するプロファイル指示",
        model="system_default、gpt-5.6-terra、gpt-5.6-lunaのいずれか",
    )
    async def upsert_chatbot_profile(
        self,
        interaction: discord.Interaction,
        profile_name: str,
        instructions: str,
        model: str = "system_default",
    ) -> None:
        await self.custom_profile_use_cases.upsert(
            interaction,
            profile_name,
            instructions,
            model,
        )

    @chatbot.command(name="profile_disable", description="Chatbotのカスタムプロファイルを無効化します")
    @app_commands.describe(profile_name="無効化するプロファイル名")
    async def disable_chatbot_profile(
        self,
        interaction: discord.Interaction,
        profile_name: str,
    ) -> None:
        await self.custom_profile_use_cases.disable(interaction, profile_name)

    @chatbot.command(name="profile_show", description="Chatbotのカスタムプロファイルを表示します")
    @app_commands.describe(profile_name="表示するプロファイル名")
    async def show_chatbot_profile(
        self,
        interaction: discord.Interaction,
        profile_name: str,
    ) -> None:
        await self.custom_profile_use_cases.show(interaction, profile_name)

    @chatbot.command(name="profile_list", description="有効なChatbotカスタムプロファイルを一覧表示します")
    async def list_chatbot_profiles(self, interaction: discord.Interaction) -> None:
        await self.custom_profile_use_cases.list_enabled(interaction)

    @chatbot.command(name="aliases_export", description="Chatbotのメンバー別名をExcelで出力します")
    async def export_chatbot_member_aliases(self, interaction: discord.Interaction) -> None:
        await self.excel_management_use_cases.export_chatbot_member_aliases(interaction)

    @chatbot.command(name="aliases_import", description="編集済みのメンバー別名Excelを取り込みます")
    async def import_chatbot_member_aliases(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        if await self._reject_debug_shared_write(interaction):
            return
        await self.excel_management_use_cases.import_chatbot_member_aliases(interaction, attachment)

    async def _reject_debug_shared_write(self, interaction: discord.Interaction) -> bool:
        """デバッグBotから長期保存データを管理更新させません。"""
        if self.runtime_environment.is_production:
            return False
        await interaction.response.send_message(
            "デバッグ環境では、本番と共有するChatbotデータを更新できません。",
            ephemeral=True,
        )
        return True


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(ChatBot(bot, session_factory))
