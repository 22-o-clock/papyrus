"""🥶が向けられた事実の記録と取り消し。"""

import datetime
from dataclasses import dataclass
from logging import getLogger

import discord
from discord.ext import commands

from cogs.cynicism.constants import CYNICISM_EMOJI, JST, REACTION_SOURCE, REPLY_SOURCE
from cogs.cynicism.repositories.configuration import CynicismConfigurationRepository
from cogs.cynicism.repositories.reaction import CynicismReactionEvent, CynicismReactionRepository
from cogs.cynicism.services.reaction_filter import is_cynicism_emoji, is_cynicism_only_content
from core.runtime_environment import RuntimeEnvironment

logger = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TargetMessage:
    """🥶を向けられた発言のうち、集計に必要な情報。"""

    author_id: int
    author_is_bot: bool
    posted_at: datetime.datetime


class CynicismTrackingUseCases:
    """Discordのリアクションと返信から冷笑ポイントの根拠を集める。"""

    def __init__(
        self,
        bot: commands.Bot,
        runtime_environment: RuntimeEnvironment,
        reaction_repository: CynicismReactionRepository,
        configuration_repository: CynicismConfigurationRepository,
    ) -> None:
        self._bot = bot
        self._runtime_environment = runtime_environment
        self._reactions = reaction_repository
        self._configuration = configuration_repository

    async def on_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """付与された🥶を冷笑ポイントの根拠として記録する。"""
        if not is_cynicism_emoji(payload.emoji) or not self._is_target_channel(payload.guild_id, payload.channel_id):
            return
        if await self._is_paused():
            return

        target = await self._resolve_target_message(
            payload.channel_id,
            payload.message_id,
            fallback_author_id=payload.message_author_id,
            guild_id=payload.guild_id,
        )
        if target is None or target.author_is_bot:
            return

        await self._reactions.record(
            CynicismReactionEvent(
                message_id=payload.message_id,
                reactor_id=payload.user_id,
                emoji_name=CYNICISM_EMOJI,
                is_burst=payload.burst,
                source=REACTION_SOURCE,
                evidence_message_id=None,
                channel_id=payload.channel_id,
                message_author_id=target.author_id,
                message_author_is_bot=target.author_is_bot,
                reactor_is_bot=self._is_bot_user(payload.user_id, payload.member),
                message_posted_at=target.posted_at,
                recorded_at=datetime.datetime.now(JST),
            )
        )

    async def on_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """取り消された🥶の記録を削除する。一時停止中でも整合のため処理する。"""
        if not is_cynicism_emoji(payload.emoji):
            return
        await self._reactions.remove_reaction(
            payload.message_id,
            payload.user_id,
            CYNICISM_EMOJI,
            is_burst=payload.burst,
        )

    async def on_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        """メッセージの全リアクションが消された場合の記録を削除する。"""
        await self._reactions.remove_message_reactions(payload.message_id)

    async def on_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        """特定の絵文字が一括削除された場合の記録を削除する。"""
        if not is_cynicism_emoji(payload.emoji):
            return
        await self._reactions.remove_emoji_reactions(payload.message_id, CYNICISM_EMOJI)

    async def on_message(self, message: discord.Message) -> None:
        """Papyrusが🥶だけを返信した場合も、リアクションと同じ根拠として記録する。"""
        reference_message_id = message.reference.message_id if message.reference is not None else None
        if (
            self._bot.user is None
            or message.author.id != self._bot.user.id
            or reference_message_id is None
            or not is_cynicism_only_content(message.content)
            or not self._is_target_channel(message.guild.id if message.guild is not None else None, message.channel.id)
        ):
            return
        if await self._is_paused():
            return

        target = await self._resolve_target_message(
            message.channel.id,
            reference_message_id,
            fallback_author_id=None,
            guild_id=message.guild.id if message.guild is not None else None,
        )
        if target is None or target.author_is_bot:
            return

        await self._reactions.record(
            CynicismReactionEvent(
                message_id=reference_message_id,
                reactor_id=self._bot.user.id,
                emoji_name=CYNICISM_EMOJI,
                is_burst=False,
                source=REPLY_SOURCE,
                evidence_message_id=message.id,
                channel_id=message.channel.id,
                message_author_id=target.author_id,
                message_author_is_bot=target.author_is_bot,
                reactor_is_bot=True,
                message_posted_at=target.posted_at,
                recorded_at=datetime.datetime.now(JST),
            )
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """根拠となった🥶だけの返信が削除された場合に、その分を取り消す。"""
        await self._reactions.remove_reply_evidence(payload.message_id)

    def _is_target_channel(self, guild_id: int | None, channel_id: int) -> bool:
        """サーバー内かつ実行環境の担当チャンネルかを返す。"""
        if guild_id is None:
            return False
        return self._runtime_environment.should_process_chatbot_channel(channel_id)

    async def _is_paused(self) -> bool:
        """集計が一時停止されているかを返す。"""
        settings = await self._configuration.get()
        return settings.is_paused

    def _is_bot_user(self, user_id: int, member: discord.Member | None) -> bool:
        """🥶を付けた相手がBotかを、取得できる情報の範囲で判定する。"""
        if member is not None:
            return member.bot
        user = self._bot.get_user(user_id)
        if user is not None:
            return user.bot
        return self._bot.user is not None and user_id == self._bot.user.id

    async def _resolve_target_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        fallback_author_id: int | None,
        guild_id: int | None,
    ) -> TargetMessage | None:
        """🥶を向けられた発言の投稿者と投稿時刻を解決する。"""
        channel = self._bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel | discord.Thread):
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.debug("Failed to fetch cynicism target message (message_id=%s)", message_id)
            else:
                return TargetMessage(
                    author_id=message.author.id,
                    author_is_bot=message.author.bot,
                    posted_at=message.created_at.astimezone(JST),
                )

        if fallback_author_id is None:
            return None
        # 取得できない場合でも、Discord IDから投稿時刻を復元して集計期間を決められる。
        guild = self._bot.get_guild(guild_id) if guild_id is not None else None
        author = guild.get_member(fallback_author_id) if guild is not None else None
        return TargetMessage(
            author_id=fallback_author_id,
            author_is_bot=author.bot if author is not None else False,
            posted_at=discord.utils.snowflake_time(message_id).astimezone(JST),
        )
