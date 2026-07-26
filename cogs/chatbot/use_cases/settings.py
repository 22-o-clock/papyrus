import discord

from cogs.chatbot.channel_roles import ChannelRole, ChannelRoleManager
from cogs.chatbot.services.response_policy import can_change_channel_role
from core.runtime_environment import RuntimeEnvironment


class SettingsUseCases:
    """Chatbotの実行設定とチャンネル別設定を管理する。"""

    def __init__(
        self,
        role_manager: ChannelRoleManager,
        runtime_environment: RuntimeEnvironment,
    ) -> None:
        self._role_manager = role_manager
        self._runtime_environment = runtime_environment

    async def initialize(self) -> None:
        """起動時に追加で読み込むグローバル設定はありません。"""

    async def show_role(self, interaction: discord.Interaction) -> None:
        if not await self._validate_chatbot_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return
        is_thread = isinstance(interaction.channel, discord.Thread)
        configured = await self._role_manager.get_configured_role(channel_id)
        role = await self._role_manager.get_role(channel_id)
        target_name = "スレッド" if is_thread else "チャンネル"
        source = f"この{target_name}の設定" if configured is not None else "既定値"
        await interaction.response.send_message(
            f"このチャンネルのChatbotの役割は `{role.value}` です。設定元: {source}。",
            ephemeral=True,
        )

    async def set_role(self, interaction: discord.Interaction, role: ChannelRole) -> None:
        if not await self._validate_chatbot_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return
        is_thread = isinstance(interaction.channel, discord.Thread)
        if not can_change_channel_role(is_thread=is_thread, manage_channels=interaction.permissions.manage_channels):
            await interaction.response.send_message(
                "通常チャンネルの役割変更には「チャンネルの管理」権限が必要です。", ephemeral=True
            )
            return
        await self._role_manager.set_role(channel_id, role)
        target_name = "スレッド" if is_thread else "チャンネル"
        await interaction.response.send_message(
            f"{interaction.user.mention} がこの{target_name}のChatbotの役割を `{role.value}` に変更しました。"
        )

    async def reset_role(self, interaction: discord.Interaction) -> None:
        if not await self._validate_chatbot_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return
        is_thread = isinstance(interaction.channel, discord.Thread)
        if not can_change_channel_role(is_thread=is_thread, manage_channels=interaction.permissions.manage_channels):
            await interaction.response.send_message(
                "通常チャンネルの役割変更には「チャンネルの管理」権限が必要です。", ephemeral=True
            )
            return
        await self._role_manager.clear_role(channel_id)
        role = await self._role_manager.get_role(channel_id)
        target_name = "スレッド" if is_thread else "チャンネル"
        await interaction.response.send_message(
            f"{interaction.user.mention} がこの{target_name}固有の設定を解除しました。"
            f"現在は `{role.value}` です。設定元: 既定値。"
        )

    async def _validate_chatbot_channel(self, interaction: discord.Interaction) -> bool:
        """実行環境でChatbot処理を許可したチャンネルだけを受け付けます。"""
        channel_id = interaction.channel_id
        if channel_id is not None and self._runtime_environment.should_process_chatbot_channel(channel_id):
            return True
        await interaction.response.send_message(
            "この実行環境では、このチャンネルのChatbot設定を操作できません。",
            ephemeral=True,
        )
        return False
