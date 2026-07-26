import os
from datetime import datetime, timedelta, timezone
from logging import getLogger

import discord
from discord import TextChannel, Thread
from discord.ext import commands, tasks

from core.runtime_environment import get_runtime_environment
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
        self.runtime_environment = get_runtime_environment()
        self.event_notify_channel = int(os.environ["CHANNEL_ID_LOBBY"])
        self.lastfm_users = parse_comma_separated_values(os.environ.get("LASTFM_IDS"))
        self.lastfm_log_thread = int(os.environ["THREAD_ID_ANTHYME_LOG"]) if self.lastfm_users else None
        self.last_warned: dict[str, datetime] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.runtime_environment.is_debug:
            logger.info("Disabled Last.fm warning worker in debug environment")
            return
        if not self.last_fm_update_warn.is_running():
            self.last_fm_update_warn.start()

    async def cog_unload(self) -> None:
        self.last_fm_update_warn.cancel()

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

    @commands.Cog.listener("on_scheduled_event_create")
    async def event_create_notify(self, event: discord.ScheduledEvent) -> None:
        if self.runtime_environment.is_debug:
            return
        channel = await _fetch_text_channel(self.bot, self.event_notify_channel)
        embed = await self._create_event_embed(event, discord.Colour.teal(), f"🗓️ イベントの作成: {event.name}", channel)
        sent_message = await channel.send(embed=embed)
        self.bot.dispatch("exclude_from_long_term_memory", sent_message)

    @commands.Cog.listener("on_scheduled_event_update")
    async def event_update_notify(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        if self.runtime_environment.is_debug:
            return
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
        sent_message = await channel.send(embed=embed)
        self.bot.dispatch("exclude_from_long_term_memory", sent_message)

    @commands.Cog.listener("on_scheduled_event_delete")
    async def event_delete_notify(self, event: discord.ScheduledEvent) -> None:
        if self.runtime_environment.is_debug:
            return
        channel = await _fetch_text_channel(self.bot, self.event_notify_channel)
        embed = await self._create_event_embed(
            event,
            discord.Colour.magenta(),
            f"🗓️ イベントの中止: {event.name}",
            channel,
        )
        sent_message = await channel.send(embed=embed)
        self.bot.dispatch("exclude_from_long_term_memory", sent_message)

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
