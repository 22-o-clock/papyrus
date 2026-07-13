import datetime

import discord
from discord import TextChannel, Thread
from discord.ext import commands

from cogs.api_usage.openai_usage import JST
from cogs.api_usage.repositories.report import ApiUsageReportDatabase, ReportDelivery

from .report_builder import REPORT_MARKER_PREFIX


class ApiUsageReportMessageDelivery:
    """Discord上の日次レポートを再発見し、投稿または更新する。"""

    def __init__(self, bot: commands.Bot, database: ApiUsageReportDatabase, target_id: int) -> None:
        self._bot = bot
        self._database = database
        self.target_id = target_id

    async def upsert(
        self,
        report_date: datetime.date,
        embed: discord.Embed,
    ) -> tuple[discord.Message, datetime.datetime]:
        """配送記録またはフッターから既存投稿を見つけ、なければ新規投稿する。"""
        target = await self._get_target()
        delivery = await self._database.get_delivery(report_date, self.target_id)
        message = await self._fetch_delivery_message(target, delivery)
        if message is None:
            message = await self._find_report_message(target, report_date)
        now = datetime.datetime.now(JST)
        if message is None:
            return await target.send(embed=embed), now
        await message.edit(embed=embed)
        return message, now

    async def _fetch_delivery_message(
        self,
        target: TextChannel | Thread,
        delivery: ReportDelivery | None,
    ) -> discord.Message | None:
        """永続化済みIDのメッセージを取得し、削除済みなら未発見として扱う。"""
        if delivery is None:
            return None
        try:
            return await target.fetch_message(delivery.message_id)
        except discord.NotFound:
            return None

    async def _find_report_message(
        self,
        target: TextChannel | Thread,
        report_date: datetime.date,
    ) -> discord.Message | None:
        """送信後DB保存前の停止に備え、直近履歴のフッター識別子から投稿を再発見する。"""
        marker = f"{REPORT_MARKER_PREFIX}{report_date.isoformat()}"
        async for message in target.history(limit=100):
            if self._bot.user is None or message.author.id != self._bot.user.id:
                continue
            if any(embed.footer.text and marker in embed.footer.text for embed in message.embeds):
                return message
        return None

    async def _get_target(self) -> TextChannel | Thread:
        """設定されたDiscord投稿先をテキストチャンネルまたはスレッドとして取得する。"""
        target = self._bot.get_channel(self.target_id) or await self._bot.fetch_channel(self.target_id)
        if not isinstance(target, (TextChannel, Thread)):
            message = f"API usage report target must be a text channel or thread: target_id={self.target_id}"
            raise TypeError(message)
        return target
