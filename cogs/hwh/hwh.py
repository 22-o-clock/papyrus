import asyncio
import os
from datetime import datetime, timedelta, timezone
from logging import getLogger

import discord
from discord import Interaction, Message, TextChannel, Thread, app_commands
from discord.ext import commands, tasks

from core.tools.ebd import make_simple_embed
from core.tools.utils import parse_comma_separated_values

from . import lastfm

logger = getLogger(__name__)

JST = timezone(timedelta(hours=9))
LASTFM_STALE_AFTER = timedelta(hours=12)
LASTFM_WARNING_INTERVAL = timedelta(hours=4)


async def _fetch_text_channel(bot: commands.Bot, channel_id: int) -> TextChannel | Thread:
    """通知先を取得し、テキスト送信できないチャンネル種別なら例外にする。"""
    channel = await bot.fetch_channel(channel_id)
    if isinstance(channel, (TextChannel, Thread)):
        return channel

    error_message = f"channel {channel_id} is not a text channel or thread"
    raise TypeError(error_message)


class Patchwork(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.event_notify_channel = int(os.environ["LOBBY"])
        self.lastfm_users = parse_comma_separated_values(os.environ.get("LASTFM_IDS"))
        self.lastfm_log_thread = int(os.environ["ANTHYME_LOG_THREAD"]) if self.lastfm_users else None
        self.last_warned: dict[str, datetime] = {}
        self.prohibited_users: dict[int, set[int]] = {}
        self.focus_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.last_fm_update_warn.is_running():
            self.last_fm_update_warn.start()

    async def cog_unload(self) -> None:
        self.last_fm_update_warn.cancel()
        for task in self.focus_tasks.values():
            task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.guild is None:
            return
        if message.author.id in self.prohibited_users.get(message.guild.id, set()):
            await message.delete()

    @tasks.loop(minutes=10)
    async def last_fm_update_warn(self) -> None:
        for user in self.lastfm_users:
            await self._warn_stale_lastfm(user)

    @last_fm_update_warn.before_loop
    async def before_last_fm_update_warn(self) -> None:
        await self.bot.wait_until_ready()

    async def _warn_stale_lastfm(self, user: str) -> None:
        """最終Scrobbleが古い場合、一定間隔を空けて警告する。"""
        now = datetime.now(JST)
        if (last_warned := self.last_warned.get(user)) and last_warned + LASTFM_WARNING_INTERVAL > now:
            return

        scrobble = await lastfm.fetch_latest_track(user)
        if scrobble is None or scrobble.time.astimezone(JST) >= now - LASTFM_STALE_AFTER:
            return
        if self.lastfm_log_thread is None:
            return

        channel = await _fetch_text_channel(self.bot, self.lastfm_log_thread)
        await channel.send(
            "⚠️ {} の last.fm は、{} の {} - {} ({}) から更新されていません。".format(
                user,
                scrobble.time.astimezone(JST).strftime("%m/%d %H:%M"),
                scrobble.artist,
                scrobble.title,
                scrobble.album,
            )
        )
        self.last_warned[user] = now

    @app_commands.command(description="指定時間が経過するまで自身の新規投稿を禁止します。")
    @app_commands.rename(minutes="min")
    @app_commands.describe(minutes="投稿を禁止する分数")
    async def stay_focused(self, interaction: Interaction, minutes: app_commands.Range[int, 1, 1440]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        key = (interaction.guild.id, interaction.user.id)
        if existing_task := self.focus_tasks.get(key):
            existing_task.cancel()

        self.prohibited_users.setdefault(interaction.guild.id, set()).add(interaction.user.id)
        self.focus_tasks[key] = asyncio.create_task(self._release_focus(*key, minutes))
        await interaction.response.send_message(f"{minutes} 分間、{interaction.user.mention} の新規投稿を禁止します 👌")

    async def _release_focus(self, guild_id: int, user_id: int, minutes: int) -> None:
        """指定時間後に投稿禁止を解除する。Cog停止時のキャンセルでは状態を破棄する。"""
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            return

        self.prohibited_users.get(guild_id, set()).discard(user_id)
        self.focus_tasks.pop((guild_id, user_id), None)

    @commands.Cog.listener("on_scheduled_event_create")
    async def event_create_notify(self, event: discord.ScheduledEvent) -> None:
        channel = await _fetch_text_channel(self.bot, self.event_notify_channel)
        embed = await self._create_event_embed(event, discord.Colour.teal(), f"🗓️ イベントの作成: {event.name}", channel)
        await channel.send(embed=embed)

    @commands.Cog.listener("on_scheduled_event_update")
    async def event_update_notify(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        if self._events_are_equal(before, after):
            return

        if before.status == after.status:
            colour = discord.Colour.lighter_gray()
            title = f"🗓️ イベントの更新: {after.name}"
        elif after.status == discord.EventStatus.active:
            colour = discord.Colour.magenta()
            title = f"🗓️ イベントの開始: {after.name}"
        elif after.status == discord.EventStatus.completed:
            colour = discord.Colour.magenta()
            title = f"🗓️ イベントの終了: {after.name}"
        else:
            return

        channel = await _fetch_text_channel(self.bot, self.event_notify_channel)
        embed = await self._create_event_embed(after, colour, title, channel)
        await channel.send(embed=embed)

    @commands.Cog.listener("on_scheduled_event_delete")
    async def event_delete_notify(self, event: discord.ScheduledEvent) -> None:
        channel = await _fetch_text_channel(self.bot, self.event_notify_channel)
        embed = await self._create_event_embed(
            event,
            discord.Colour.magenta(),
            f"🗓️ イベントの中止: {event.name}",
            channel,
        )
        await channel.send(embed=embed)

    async def _create_event_embed(
        self,
        event: discord.ScheduledEvent,
        colour: discord.Colour,
        title: str,
        channel: TextChannel | Thread,
    ) -> discord.Embed:
        """予定イベント通知に共通するEmbedを作成する。"""
        embed = make_simple_embed(colour, title, event.description or "", url=event.url)

        creator = event.creator
        if creator is None and event.creator_id is not None and event.guild is not None:
            creator = event.guild.get_member(event.creator_id)
            if creator is None:
                creator = await event.guild.fetch_member(event.creator_id)
        if creator is not None:
            embed.set_author(name=creator.display_name, icon_url=creator.display_avatar.url)

        if event.entity_type in (discord.EntityType.voice, discord.EntityType.stage_instance):
            event_channel = event.channel
            if event_channel is not None:
                embed.set_footer(
                    text=f"scheduled for {event.start_time.astimezone(JST):%m/%d %H:%M} at 🔉{event_channel.name}",
                    icon_url=channel.guild.icon.url if channel.guild.icon else "",
                )
        return embed

    @staticmethod
    def _events_are_equal(before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> bool:
        """通知対象となる予定イベントの主要属性が同一か判定する。"""
        return (
            before.name == after.name
            and before.description == after.description
            and before.entity_type == after.entity_type
            and before.channel_id == after.channel_id
            and before.location == after.location
            and before.start_time == after.start_time
            and before.status == after.status
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Patchwork(bot))
    logger.debug("%s is added to the bot.", __name__)
