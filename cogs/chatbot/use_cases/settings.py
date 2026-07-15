from logging import getLogger

import discord

from cogs.chatbot.channel_roles import ChannelRole, ChannelRoleManager
from cogs.chatbot.constants import (
    CONVERSATION_RESET_MINUTES_KEY,
    DEFAULT_CONVERSATION_RESET_MINUTES,
    DEFAULT_UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES,
    DEFAULT_UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES,
    MINIMUM_CONVERSATION_RESET_MINUTES,
    UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES_KEY,
    UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES_KEY,
)
from cogs.chatbot.repositories.environment import DatabaseEnvironmentRepository
from cogs.chatbot.services.response_policy import can_change_channel_role
from cogs.chatbot.shadow_mode import ShadowModeManager
from core.runtime_environment import RuntimeEnvironment

logger = getLogger(__name__)


class SettingsUseCases:
    """Chatbotの実行設定とチャンネル別設定を管理する。"""

    def __init__(
        self,
        environment_repository: DatabaseEnvironmentRepository,
        role_manager: ChannelRoleManager,
        shadow_mode_manager: ShadowModeManager,
        runtime_environment: RuntimeEnvironment,
    ) -> None:
        self._environment_repository = environment_repository
        self._role_manager = role_manager
        self._shadow_mode_manager = shadow_mode_manager
        self._runtime_environment = runtime_environment
        self.conversation_reset_minutes = DEFAULT_CONVERSATION_RESET_MINUTES
        self.unanswered_question_minimum_wait_minutes = DEFAULT_UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES
        self.unanswered_question_maximum_wait_minutes = DEFAULT_UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES

    async def initialize(self) -> bool:
        """保存済み設定を読み込み、質問待機時間が有効ならTrueを返す。"""
        self.conversation_reset_minutes = await self._load_positive_minutes(
            CONVERSATION_RESET_MINUTES_KEY,
            DEFAULT_CONVERSATION_RESET_MINUTES,
        )
        minimum = await self._load_positive_minutes(
            UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES_KEY,
            DEFAULT_UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES,
        )
        maximum = await self._load_positive_minutes(
            UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES_KEY,
            DEFAULT_UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES,
        )
        if minimum > maximum:
            logger.warning("Invalid chatbot unanswered question wait range (minimum=%s, maximum=%s)", minimum, maximum)
            return False
        self.unanswered_question_minimum_wait_minutes = minimum
        self.unanswered_question_maximum_wait_minutes = maximum
        return True

    async def _load_positive_minutes(self, key: str, default: int) -> int:
        configured = await self._environment_repository.get_env(key)
        if configured is None:
            return default
        try:
            minutes = int(configured)
        except ValueError:
            logger.warning("Invalid chatbot minutes setting (key=%s, value=%r)", key, configured)
            return default
        if minutes < MINIMUM_CONVERSATION_RESET_MINUTES:
            logger.warning("Chatbot minutes setting is too small (key=%s, value=%s)", key, minutes)
            return default
        return minutes

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

    async def set_conversation_reset_minutes(self, interaction: discord.Interaction, minutes: int) -> None:
        if not await self._validate_shared_write(interaction):
            return
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "会話リセット時間の変更には「サーバー管理」権限が必要です。", ephemeral=True
            )
            return
        if minutes < MINIMUM_CONVERSATION_RESET_MINUTES:
            await interaction.response.send_message("会話リセット時間は1分以上で指定してください。", ephemeral=True)
            return
        previous = self.conversation_reset_minutes
        self.conversation_reset_minutes = minutes
        await self._environment_repository.set_env(CONVERSATION_RESET_MINUTES_KEY, str(minutes))
        await interaction.response.send_message(f"Chatbotの会話リセット時間を {previous}分から {minutes}分に変更しました。")

    async def set_question_wait(self, interaction: discord.Interaction, minimum: int, maximum: int) -> None:
        if not await self._validate_shared_write(interaction):
            return
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "質問への回答待機時間の変更には「サーバー管理」権限が必要です。", ephemeral=True
            )
            return
        if minimum < MINIMUM_CONVERSATION_RESET_MINUTES or minimum > maximum:
            await interaction.response.send_message(
                "待機時間は「1以上の最短分数」と「最短以上の最長分数」で指定してください。", ephemeral=True
            )
            return
        previous_minimum = self.unanswered_question_minimum_wait_minutes
        previous_maximum = self.unanswered_question_maximum_wait_minutes
        self.unanswered_question_minimum_wait_minutes = minimum
        self.unanswered_question_maximum_wait_minutes = maximum
        await self._environment_repository.set_env(UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES_KEY, str(minimum))
        await self._environment_repository.set_env(UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES_KEY, str(maximum))
        await interaction.response.send_message(
            "宛先のない質問への回答待機時間を "
            f"{previous_minimum}〜{previous_maximum}分から {minimum}〜{maximum}分に変更しました。"
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

    async def set_shadow_mode(self, interaction: discord.Interaction, *, enabled: bool) -> None:
        if not await self._validate_chatbot_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return
        is_thread = isinstance(interaction.channel, discord.Thread)
        if not can_change_channel_role(is_thread=is_thread, manage_channels=interaction.permissions.manage_channels):
            await interaction.response.send_message(
                "通常チャンネルのシャドーモード変更には「チャンネルの管理」権限が必要です。", ephemeral=True
            )
            return
        await self._shadow_mode_manager.set_enabled(channel_id, enabled=enabled)
        state_text = "有効" if enabled else "無効"
        await interaction.response.send_message(f"このチャンネルのChatbotシャドーモードを{state_text}にしました。")

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

    async def _validate_shared_write(self, interaction: discord.Interaction) -> bool:
        """デバッグBotから本番共通のグローバル設定を変更させません。"""
        if self._runtime_environment.is_production:
            return True
        await interaction.response.send_message(
            "デバッグ環境では、本番と共有するChatbot設定を変更できません。",
            ephemeral=True,
        )
        return False
