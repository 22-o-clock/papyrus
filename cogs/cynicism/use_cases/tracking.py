"""🥶が向けられた事実の記録と取り消し。"""

import discord
from discord.ext import commands

from cogs.cynicism.repositories.configuration import CynicismConfigurationRepository
from cogs.cynicism.repositories.reaction import CynicismReactionEvent, CynicismReactionRepository
from cogs.cynicism.services.reaction_filter import is_cynicism_emoji
from core.runtime_environment import RuntimeEnvironment


class CynicismTrackingUseCases:
    """Discordのリアクションから冷笑ポイントの根拠を集める。

    発言者や投稿時刻は集計時にTalkDataから解決するため、記録時にDiscordのメッセージは取得しない。
    """

    def __init__(
        self,
        bot: commands.Bot,
        runtime_environment: RuntimeEnvironment,
        reaction_repository: CynicismReactionRepository,
        configuration_repository: CynicismConfigurationRepository,
    ) -> None:
        """Botと処理範囲、リアクション・設定リポジトリを受け取る。"""
        self._bot = bot
        self._runtime_environment = runtime_environment
        self._reactions = reaction_repository
        self._configuration = configuration_repository

    async def on_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """付与された🥶を冷笑ポイントの根拠として記録する。"""
        if not is_cynicism_emoji(payload.emoji) or not self._is_target_channel(payload.guild_id, payload.channel_id):
            return
        # Botによる🥶は記録しない。
        if self._is_bot(payload.user_id, payload.member):
            return
        # Botの発言はランキングの対象にしない。判別できない場合も集計時のメンバー解決で除外される。
        if self._is_known_bot(payload.guild_id, payload.message_author_id):
            return
        if await self._is_paused():
            return

        await self._reactions.record(
            CynicismReactionEvent(
                message_id=payload.message_id,
                reactor_id=payload.user_id,
                is_burst=payload.burst,
            )
        )

    async def on_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """取り消された🥶の記録を削除する。一時停止中でも整合のため処理する。"""
        if not is_cynicism_emoji(payload.emoji):
            return
        await self._reactions.remove_reaction(payload.message_id, payload.user_id, is_burst=payload.burst)

    async def on_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        """メッセージの全リアクションが消された場合の記録を削除する。"""
        await self._reactions.remove_message_reactions(payload.message_id)

    async def on_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        """特定の絵文字が一括削除された場合の記録を削除する。"""
        if not is_cynicism_emoji(payload.emoji):
            return
        await self._reactions.remove_message_reactions(payload.message_id)

    def _is_target_channel(self, guild_id: int | None, channel_id: int) -> bool:
        """サーバー内かつ実行環境の担当チャンネルかを返す。"""
        if guild_id is None:
            return False
        return self._runtime_environment.should_process_chatbot_channel(channel_id)

    async def _is_paused(self) -> bool:
        """集計が一時停止されているかを返す。"""
        settings = await self._configuration.get()
        return settings.is_paused

    def _is_bot(self, user_id: int, member: discord.Member | None) -> bool:
        """Botによる操作かを、取得できる情報の範囲で判定する。"""
        if self._bot.user is not None and user_id == self._bot.user.id:
            return True
        if member is not None:
            return member.bot
        user = self._bot.get_user(user_id)
        return user is not None and user.bot

    def _is_known_bot(self, guild_id: int | None, user_id: int | None) -> bool:
        """キャッシュから確実にBotと分かる利用者かを返す。"""
        if user_id is None:
            return False
        guild = self._bot.get_guild(guild_id) if guild_id is not None else None
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return member.bot
        user = self._bot.get_user(user_id)
        return user is not None and user.bot
