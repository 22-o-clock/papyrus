"""ランキング発表メッセージの投稿と更新。"""

import datetime
from dataclasses import dataclass

import discord
from discord import TextChannel, Thread
from discord.ext import commands

from cogs.cynicism.constants import JST
from cogs.cynicism.periods import CynicismPeriod
from cogs.cynicism.repositories.report import CynicismReportRepository, ReportDelivery

from .report_builder import report_marker

HISTORY_SEARCH_LIMIT = 100


class ReportMessageOwnershipError(RuntimeError):
    """配送記録が別BotのDiscord投稿を指している場合の設定エラー。"""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """投稿または更新の結果。"""

    message: discord.Message
    updated_at: datetime.datetime
    changed: bool


class CynicismReportMessageDelivery:
    """Discord上のランキング発表を再発見し、投稿または更新する。"""

    def __init__(self, bot: commands.Bot, repository: CynicismReportRepository, target_id: int) -> None:
        self._bot = bot
        self._repository = repository
        self.target_id = target_id

    async def upsert(self, period: CynicismPeriod, embed: discord.Embed, digest: str) -> DeliveryResult:
        """配送記録またはフッターから既存投稿を見つけ、なければ新規投稿する。"""
        target = await self._get_target()
        delivery = await self._repository.get_delivery(period, self.target_id)
        message = await self._fetch_delivery_message(target, delivery)
        if message is None:
            message = await self._find_report_message(target, period)
        now = datetime.datetime.now(JST)

        if message is None:
            posted = await target.send(embed=embed)
            self._exclude_from_long_term_memory(posted)
            return DeliveryResult(posted, now, changed=True)

        self.validate_message_owner(message)
        if delivery is not None and delivery.message_id == message.id and delivery.content_digest == digest:
            # 再集計しても内容が変わらない場合は、毎分の更新でDiscordを叩かない。
            return DeliveryResult(message, delivery.last_processed_at, changed=False)

        await message.edit(embed=embed)
        self._exclude_from_long_term_memory(message)
        return DeliveryResult(message, now, changed=True)

    def validate_message_owner(self, message: discord.Message) -> None:
        """別Botの投稿を上書きせず、設定誤りとして停止する。"""
        bot_user = self._bot.user
        if bot_user is not None and message.author.id == bot_user.id:
            return
        bot_user_id = bot_user.id if bot_user is not None else None
        error_message = (
            "Cynicism report delivery belongs to another Bot "
            f"(target_id={self.target_id}, message_id={message.id}, "
            f"message_author_id={message.author.id}, bot_user_id={bot_user_id})"
        )
        raise ReportMessageOwnershipError(error_message)

    def _exclude_from_long_term_memory(self, message: discord.Message) -> None:
        """自動生成した発表投稿を、Chatbotの長期記憶の根拠から外す。"""
        self._bot.dispatch("exclude_from_long_term_memory", message)

    async def _fetch_delivery_message(
        self,
        target: TextChannel | Thread,
        delivery: ReportDelivery | None,
    ) -> discord.Message | None:
        """永続化済みIDのメッセージを取得し、削除済みなら未発見として扱う。"""
        # 投稿しなかった期間の記録にはメッセージIDが無い。
        if delivery is None or delivery.message_id is None:
            return None
        try:
            return await target.fetch_message(delivery.message_id)
        except discord.NotFound:
            return None

    async def _find_report_message(
        self,
        target: TextChannel | Thread,
        period: CynicismPeriod,
    ) -> discord.Message | None:
        """送信後DB保存前の停止に備え、直近履歴のフッター識別子から投稿を再発見する。"""
        marker = report_marker(period)
        async for message in target.history(limit=HISTORY_SEARCH_LIMIT):
            if self._bot.user is None or message.author.id != self._bot.user.id:
                continue
            if any(embed.footer.text and marker in embed.footer.text for embed in message.embeds):
                return message
        return None

    async def _get_target(self) -> TextChannel | Thread:
        """設定されたDiscord投稿先をテキストチャンネルまたはスレッドとして取得する。"""
        target = self._bot.get_channel(self.target_id) or await self._bot.fetch_channel(self.target_id)
        if not isinstance(target, TextChannel | Thread):
            message = f"Cynicism report target must be a text channel or thread: target_id={self.target_id}"
            raise TypeError(message)
        return target
