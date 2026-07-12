import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from logging import getLogger

from discord import Colour, Interaction, TextChannel, Thread, app_commands
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.remind.database import ReminderDatabase, ReminderTable
from core.exception.exception import ArgumentError
from core.tools.ebd import add_timestamp_footer, make_simple_embed

logger = getLogger(__name__)

JST = timezone(timedelta(hours=9))
MAX_REMINDER_INDEX_LENGTH = 10


class Notify(commands.Cog):
    pat1 = re.compile(r"^(?:(\d+)/(\d+)\s+)*(\d+):(\d+)\s+(.+)$")  # 08/31 03:09, 03:09
    pat2 = re.compile(r"^(?:(\d+)h)*(?:(\d+)m)*\s+(.+)$")  # 8h31m, 8h, 31m

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.db = ReminderDatabase(session_factory)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.loop_wrapper.is_running():
            self.loop_wrapper.start()

    @tasks.loop(seconds=20)
    async def loop_wrapper(self) -> None:
        await self.check_all(self.bot)
        await self.check_behind(self.bot)

    @app_commands.command(name="remind", description="リマインダーを設定します。")
    @app_commands.describe(option="リマインダーの設定文字列")
    async def remind(self, interaction: Interaction, option: str) -> None:
        """リマインダーを設定します。直接日時を指定することも，現時点からの増分で指定することもできます。"""
        now = datetime.now(tz=JST)
        des, content = None, None

        if match1 := self.pat1.match(option):
            month, day, hour, minute, content = match1.groups()
            des = now.replace(hour=int(hour), minute=int(minute))

            if month is not None and day is not None:
                des = des.replace(month=int(month), day=int(day))
                if des < now:
                    des = des.replace(year=now.year + 1)
            elif des < now:
                des = des.replace(day=now.day + 1)

        elif match2 := self.pat2.match(option):
            hour, minute, content = match2.groups()

            if hour is not None or minute is not None:
                des = now + timedelta(hours=int(hour) if hour else 0, minutes=int(minute) if minute else 0)

        if des is None or content is None:
            error_message = (
                "option は下記の形式で指定してください... 💦\n"
                "`[option] : <time> <content>`\n"
                "`   <time>   : ex. 08/31 03:09, 03:09, 3h9m, 3h, 9m`\n"
                "`   <content>: reminder text`"
            )
            raise ArgumentError(error_message)

        if interaction.channel_id is None:
            error_message = "チャンネル情報を取得できませんでした... 💦"
            raise ArgumentError(error_message)
        await interaction.response.send_message(f"{des.strftime('%m/%d %H:%M')} に {content} のリマインダーを設定しました！")
        await self.db.add_reminder(interaction.user.id, interaction.channel_id, content, des)

    @app_commands.command(name="show_reminder", description="登録されたリマインダーを表示します。")
    async def show_reminder(self, interaction: Interaction) -> None:
        embed = make_simple_embed(
            Colour.dark_blue(),
            f"{interaction.user.display_name} の登録済リマインダー ⏰",
        )

        for count, rem in enumerate(
            sorted(
                [rem for rem in await self.db.get_all_reminders() if rem.author == interaction.user.id], key=lambda x: x.time
            )
        ):
            embed.add_field(
                name=f"No.{count}",
                value=f"{rem.time.strftime('%m/%d %H:%M %z')} -> {rem.content}",
                inline=False,
            )
        embed = add_timestamp_footer(self.bot, embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="remove_reminder", description="登録されたリマインダーを削除します。")
    @app_commands.describe(indicator="リマインダーの番号を指定")
    async def remove_reminder(self, interaction: Interaction, indicator: str) -> None:
        if indicator.isdecimal() and len(indicator) <= MAX_REMINDER_INDEX_LENGTH:
            try:
                rem = sorted(
                    [rem for rem in await self.db.get_all_reminders() if rem.author == interaction.user.id],
                    key=lambda x: x.time,
                )[int(indicator)]
            except IndexError:
                error_message = "指定した番号のリマインダーは存在しません... 💦"
                raise ArgumentError(error_message) from None
        else:
            try:
                reminder_id = uuid.UUID(indicator)
            except ValueError:
                error_message = "リマインダーの番号またはUUIDを指定してください... 💦"
                raise ArgumentError(error_message) from None
            rem = await self.db.get_reminder(reminder_id, interaction.user.id)
            if rem is None:
                error_message = "指定した id のリマインダーは存在しません... 💦"
                raise ArgumentError(error_message)

        await interaction.response.send_message(
            f"{rem.time.strftime('%m/%d %H:%M')} のリマインダー: {rem.content} を削除します 👌"
        )
        await self.db.remove(rem.id)

    async def check_all(self, bot: commands.Bot) -> None:
        """現在の時刻に設定されたリマインダーがあれば、メッセージを送信して DB から削除します。"""
        now: datetime = datetime.now(tz=JST)

        for reminder in await self.db.get_all_reminders():
            if reminder.almost_equal(now):
                await asyncio.gather(
                    asyncio.create_task(self.send(bot, reminder)),
                    asyncio.create_task(self.db.remove(reminder.id)),
                )

    async def check_behind(self, bot: commands.Bot) -> None:
        """送信に失敗しているリマインダーがあれば、メッセージを送信して DB から削除します。"""
        now: datetime = datetime.now(tz=JST)

        # dictionary size must not be changed during iteration although there is remove
        for reminder in await self.db.get_all_reminders():
            if reminder.behind(now):
                await asyncio.gather(
                    asyncio.create_task(self.send(bot, reminder, on_time=False)),
                    asyncio.create_task(self.db.remove(reminder.id)),
                )

    async def send(self, bot: commands.Bot, reminder: ReminderTable, *, on_time: bool = True) -> None:
        """リマインダーの内容を送信します。"""
        channel = bot.get_channel(reminder.channel) or await bot.fetch_channel(reminder.channel)
        if not isinstance(channel, (TextChannel, Thread)):
            logger.error(
                "Failed to send reminder; unexpected channel type: channel_id=%s type=%s",
                reminder.channel,
                type(channel),
            )
            return

        if on_time:
            content = f"<@{reminder.author}> 指定の時刻です: {reminder.content}"
        else:
            content = (
                f"<@{reminder.author}> {reminder.time:%H:%M} "
                f"に設定されたリマインダー: {reminder.content} の送信に失敗していました... 💦"
            )
        await channel.send(content)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(Notify(bot, session_factory=session_factory))
    logger.debug("%s is added to the bot.", __name__)
