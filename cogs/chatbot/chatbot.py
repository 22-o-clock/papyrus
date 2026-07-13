import discord
from discord import Message, app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .channel_roles import ChannelRole
from .use_cases.conversation import ConversationUseCases
from .use_cases.excel_management import ExcelManagementUseCases


class ChatBot(commands.Cog):
    """DiscordイベントとChatbotユースケースを接続するController。"""

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.conversation_use_cases = ConversationUseCases(bot, session_factory)
        self.settings_use_cases = self.conversation_use_cases.settings_use_cases
        self.custom_profile_use_cases = self.conversation_use_cases.custom_profile_use_cases
        self.excel_management_use_cases = ExcelManagementUseCases(session_factory)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.conversation_use_cases.on_ready()

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        await self.conversation_use_cases.on_message(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message: Message) -> None:
        await self.conversation_use_cases.on_message_delete(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        await self.conversation_use_cases.on_message_edit(before, after)

    @app_commands.command(name="show_chatbot_role", description="このチャンネルでのChatbotの役割を表示します")
    async def show_chatbot_role(self, interaction: discord.Interaction) -> None:
        await self.settings_use_cases.show_role(interaction)

    @app_commands.command(name="set_chatbot_reset_minutes", description="Chatbotの会話リセット時間を変更します")
    @app_commands.describe(minutes="最後の人間投稿から会話をリセットするまでの分数 (1以上)")
    async def set_chatbot_conversation_reset_minutes(
        self,
        interaction: discord.Interaction,
        minutes: int,
    ) -> None:
        await self.settings_use_cases.set_conversation_reset_minutes(interaction, minutes)

    @app_commands.command(name="set_chatbot_question_wait", description="宛先のない質問への回答待機時間を変更します")
    @app_commands.describe(
        minimum_minutes="最短待機時間 (分、1以上)",
        maximum_minutes="最長待機時間 (分、最短以上)",
    )
    async def set_chatbot_question_wait(
        self,
        interaction: discord.Interaction,
        minimum_minutes: int,
        maximum_minutes: int,
    ) -> None:
        await self.settings_use_cases.set_question_wait(interaction, minimum_minutes, maximum_minutes)

    @app_commands.command(name="set_chatbot_role", description="このチャンネルでのChatbotの役割を変更します")
    @app_commands.describe(role="assistant または chat を選択します")
    async def set_chatbot_role(self, interaction: discord.Interaction, role: ChannelRole) -> None:
        await self.settings_use_cases.set_role(interaction, role)

    @app_commands.command(name="reset_chatbot_role", description="このチャンネル固有のChatbot役割を解除します")
    async def reset_chatbot_role(self, interaction: discord.Interaction) -> None:
        await self.settings_use_cases.reset_role(interaction)

    @app_commands.command(name="upsert_chatbot_profile", description="Chatbotのカスタムプロファイルを保存します")
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

    @app_commands.command(name="disable_chatbot_profile", description="Chatbotのカスタムプロファイルを無効化します")
    @app_commands.describe(profile_name="無効化するプロファイル名")
    async def disable_chatbot_profile(
        self,
        interaction: discord.Interaction,
        profile_name: str,
    ) -> None:
        await self.custom_profile_use_cases.disable(interaction, profile_name)

    @app_commands.command(name="show_chatbot_profile", description="Chatbotのカスタムプロファイルを表示します")
    @app_commands.describe(profile_name="表示するプロファイル名")
    async def show_chatbot_profile(
        self,
        interaction: discord.Interaction,
        profile_name: str,
    ) -> None:
        await self.custom_profile_use_cases.show(interaction, profile_name)

    @app_commands.command(name="list_chatbot_profiles", description="有効なChatbotカスタムプロファイルを一覧表示します")
    async def list_chatbot_profiles(self, interaction: discord.Interaction) -> None:
        await self.custom_profile_use_cases.list_enabled(interaction)

    @app_commands.command(name="set_chatbot_shadow_mode", description="このチャンネルのChatbotシャドーモードを変更します")
    async def set_chatbot_shadow_mode(self, interaction: discord.Interaction, *, enabled: bool) -> None:
        await self.settings_use_cases.set_shadow_mode(interaction, enabled=enabled)

    @app_commands.command(name="export_chatbot_shadow_candidates", description="未評価のChatbotシャドー候補をExcelで出力します")
    async def export_chatbot_shadow_candidates(self, interaction: discord.Interaction) -> None:
        await self.excel_management_use_cases.export_chatbot_shadow_candidates(interaction)

    @app_commands.command(name="import_chatbot_shadow_reviews", description="評価済みのChatbotシャドー候補Excelを取り込みます")
    async def import_chatbot_shadow_evaluations(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        await self.excel_management_use_cases.import_chatbot_shadow_evaluations(interaction, attachment)

    @app_commands.command(name="export_chatbot_member_aliases", description="Chatbotのメンバー別名をExcelで出力します")
    async def export_chatbot_member_aliases(self, interaction: discord.Interaction) -> None:
        await self.excel_management_use_cases.export_chatbot_member_aliases(interaction)

    @app_commands.command(name="import_chatbot_member_aliases", description="編集済みのメンバー別名Excelを取り込みます")
    async def import_chatbot_member_aliases(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        await self.excel_management_use_cases.import_chatbot_member_aliases(interaction, attachment)

    @app_commands.command(name="export_chatbot_memories", description="Chatbotの長期記憶をExcelで出力します")
    async def export_chatbot_memories(self, interaction: discord.Interaction) -> None:
        await self.excel_management_use_cases.export_chatbot_memories(interaction)

    @app_commands.command(name="import_chatbot_memories", description="編集済みの長期記憶Excelを取り込みます")
    async def import_chatbot_memories(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        await self.excel_management_use_cases.import_chatbot_memories(interaction, attachment)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(ChatBot(bot, session_factory))
