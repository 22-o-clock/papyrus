import io
from collections.abc import Collection

import discord

from cogs.chatbot.constants import DISCORD_RESPONSE_CHUNK_LENGTH
from cogs.chatbot.models.custom_profile import CustomProfile
from cogs.chatbot.repositories.custom_profile import CustomProfileRepository
from cogs.chatbot.services.custom_profile_parser import (
    PROFILE_NAME_PATTERN,
    InvalidCustomProfileDirectiveError,
    parse_custom_profile_directive,
)
from core.runtime_environment import RuntimeEnvironment

CUSTOM_PROFILE_MODELS = {"system_default", "gpt-5.6-terra", "gpt-5.6-luna"}


class CustomProfileNotFoundError(LookupError):
    """指定されたカスタムプロファイルがDBに存在しない場合の例外。"""


class CustomProfileUseCases:
    """明示されたoptionを1回分の生成設定へ解決します。"""

    def __init__(self, repository: CustomProfileRepository, runtime_environment: RuntimeEnvironment) -> None:
        self._repository = repository
        self._runtime_environment = runtime_environment

    async def resolve(
        self,
        content: str,
        *,
        message_id: int,
        bot_user_id: int,
        directly_mentioned: bool,
        bot_role_ids: Collection[int] = (),
    ) -> CustomProfile | None:
        """投稿のoption指定を検証し、DBのプロファイルと結合します。"""
        directive = parse_custom_profile_directive(
            content,
            bot_user_id=bot_user_id,
            directly_mentioned=directly_mentioned,
            bot_role_ids=bot_role_ids,
        )
        if directive is None:
            return None

        stored_profile = await self._repository.get(directive.name, enabled_only=True)
        if stored_profile is None:
            raise CustomProfileNotFoundError(directive.name)
        return CustomProfile(
            name=stored_profile.name,
            instructions=stored_profile.instructions,
            model=stored_profile.model,
            request_message_id=message_id,
            request_content=directive.content,
        )

    async def upsert(
        self,
        interaction: discord.Interaction,
        profile_name: str,
        instructions: str,
        model: str,
    ) -> None:
        """管理権限を確認し、プロファイルを作成または更新します。"""
        if not await self._validate_shared_write(interaction):
            return
        if not await self._validate_admin(interaction):
            return
        normalized_name = profile_name.lower()
        if PROFILE_NAME_PATTERN.fullmatch(normalized_name) is None:
            await interaction.response.send_message(
                "プロファイル名には英数字、_、-だけを使用できます。",
                ephemeral=True,
            )
            return
        if model not in CUSTOM_PROFILE_MODELS:
            await interaction.response.send_message(
                "モデルは system_default、gpt-5.6-terra、gpt-5.6-lunaから選択してください。",
                ephemeral=True,
            )
            return
        await self._repository.upsert(
            normalized_name,
            instructions,
            model,
            user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"カスタムプロファイル {normalized_name} を保存しました。",
            ephemeral=True,
        )

    async def disable(self, interaction: discord.Interaction, profile_name: str) -> None:
        """管理権限を確認し、プロファイルを無効化します。"""
        if not await self._validate_shared_write(interaction):
            return
        if not await self._validate_admin(interaction):
            return
        normalized_name = profile_name.lower()
        if not await self._repository.disable(normalized_name, user_id=interaction.user.id):
            await interaction.response.send_message(
                f"カスタムプロファイル {normalized_name} は見つかりませんでした。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"カスタムプロファイル {normalized_name} を無効化しました。",
            ephemeral=True,
        )

    async def show(self, interaction: discord.Interaction, profile_name: str) -> None:
        """管理権限を確認し、プロファイルの設定内容を表示します。"""
        if not await self._validate_admin(interaction):
            return
        normalized_name = profile_name.lower()
        profile = await self._repository.get(normalized_name)
        if profile is None:
            await interaction.response.send_message(
                f"カスタムプロファイル {normalized_name} は見つかりませんでした。",
                ephemeral=True,
            )
            return
        status = "有効" if profile.enabled else "無効"
        body = f"name: {profile.name}\nmodel: {profile.model}\nstatus: {status}\n\n{profile.instructions}"
        if len(body) <= DISCORD_RESPONSE_CHUNK_LENGTH:
            await interaction.response.send_message(body, ephemeral=True)
            return
        attachment = discord.File(
            io.BytesIO(body.encode("utf-8")),
            filename=f"chatbot_profile_{profile.name}.txt",
        )
        await interaction.response.send_message(file=attachment, ephemeral=True)

    async def list_enabled(self, interaction: discord.Interaction) -> None:
        """管理権限を確認し、有効なプロファイル名とモデルを一覧表示します。"""
        if not await self._validate_admin(interaction):
            return
        profiles = await self._repository.list_enabled()
        if not profiles:
            await interaction.response.send_message(
                "有効なカスタムプロファイルはありません。",
                ephemeral=True,
            )
            return
        lines = [f"- {profile.name}: {profile.model}" for profile in profiles]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _validate_shared_write(self, interaction: discord.Interaction) -> bool:
        """デバッグBotから本番共通のプロファイルを変更させません。"""
        if self._runtime_environment.is_production:
            return True
        await interaction.response.send_message(
            "デバッグ環境では、本番と共有するカスタムプロファイルを変更できません。",
            ephemeral=True,
        )
        return False

    @staticmethod
    async def _validate_admin(interaction: discord.Interaction) -> bool:
        """サーバー管理権限がなければエラーを返します。"""
        if interaction.permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "カスタムプロファイルの管理には「サーバー管理」権限が必要です。",
            ephemeral=True,
        )
        return False


__all__ = [
    "CustomProfileNotFoundError",
    "CustomProfileUseCases",
    "InvalidCustomProfileDirectiveError",
]
