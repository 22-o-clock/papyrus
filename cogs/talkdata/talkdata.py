import os
import re
from datetime import datetime, timedelta, timezone
from logging import getLogger

from discord import ForumChannel, Interaction, Member, Message, RawMessageUpdateEvent, TextChannel, Thread, app_commands
from discord.ext import commands, tasks
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.talkdata.database import (
    DiscordChannel,
    DiscordMember,
    DiscordMessage,
    TalkDataDatabase,
    TalkDataNotFoundError,
)
from core.exception import MissingRequiredRoleError

logger = getLogger(__name__)
JST = timezone(timedelta(hours=9))
type MessageChannel = TextChannel | Thread
DELETED_STATUS = 1
EDITED_STATUS = 2
REPLY_SUMMARY_LENGTH = 20
MAX_FAILED_CONNECTION_CHECKS = 5


def format_upsert_result(success: list[str], errors: list[str], target_name: str) -> str:
    """upsert結果のうち、対象が存在する区分だけをメッセージに含める。"""
    sections: list[str] = []
    if success:
        sections.append(f"以下の{target_name}を登録しました\n{'\n'.join(success)}")
    if errors:
        sections.append(f"以下の{target_name}は登録できませんでした\n{'\n'.join(errors)}")
    return "\n".join(sections)


def _require_message_channel(channel: object) -> MessageChannel:
    if isinstance(channel, (TextChannel, Thread)):
        return channel
    error_message = f"TextChannelまたはThreadが必要です: {type(channel)}"
    raise TypeError(error_message)


async def _latest_message(session: AsyncSession, message_id: int) -> DiscordMessage | None:
    return await session.scalar(
        select(DiscordMessage).where(DiscordMessage.id == message_id).order_by(DiscordMessage.edit_count.desc())
    )


async def insert_message(session: AsyncSession, message: Message) -> None:
    """メッセージの現在状態を、編集履歴を保持したまま保存する。"""
    channel = _require_message_channel(message.channel)
    previous = await _latest_message(session, message.id)
    if previous is not None and message.content == previous.content:
        return

    member_statement = postgresql_insert(DiscordMember).values(
        id=message.author.id,
        display_name=message.author.display_name,
    )
    await session.execute(
        member_statement.on_conflict_do_update(
            index_elements=[DiscordMember.id],
            set_={"display_name": member_statement.excluded.display_name},
        )
    )

    if isinstance(channel, Thread):
        if channel.parent is None:
            error_message = "スレッドの親チャンネルを取得できませんでした。"
            raise TypeError(error_message)
        parent_statement = postgresql_insert(DiscordChannel).values(
            id=channel.parent_id,
            name=channel.parent.name,
            parent_id=0,
        )
        await session.execute(
            parent_statement.on_conflict_do_update(
                index_elements=[DiscordChannel.id],
                set_={"name": parent_statement.excluded.name, "parent_id": parent_statement.excluded.parent_id},
            )
        )
        channel_statement = postgresql_insert(DiscordChannel).values(
            id=channel.id,
            name=channel.name,
            parent_id=channel.parent_id,
        )
    else:
        channel_statement = postgresql_insert(DiscordChannel).values(id=channel.id, name=channel.name, parent_id=0)
    await session.execute(
        channel_statement.on_conflict_do_update(
            index_elements=[DiscordChannel.id],
            set_={"name": channel_statement.excluded.name, "parent_id": channel_statement.excluded.parent_id},
        )
    )

    reply_id = message.reference.message_id if message.reference and message.reference.message_id else 0
    reply = await _latest_message(session, reply_id) if reply_id else None
    session.add(
        DiscordMessage(
            id=message.id,
            edit_count=previous.edit_count + 1 if previous else 0,
            channel_id=channel.id,
            member_id=message.author.id,
            reply_id=reply_id,
            reply_edit_count=reply.edit_count if reply else 0,
            content=message.content,
            attachment=",".join(str(attachment.url) for attachment in message.attachments),
            post_time=(message.edited_at or message.created_at).astimezone(JST),
        )
    )
    if previous is not None:
        previous.status = 2
    await session.commit()


async def insert_channel_history(session: AsyncSession, channel: MessageChannel) -> None:
    """指定チャンネルの取得可能な全履歴を古い順に保存する。"""
    async for message in channel.history(limit=None, oldest_first=True):
        await insert_message(session, message)


class TalkData(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.db = TalkDataDatabase(session_factory)
        self.admin_role_id = int(os.environ["BOT_ADMIN"])
        self.failed_connection_checks = 0
        self.message_history_menu = app_commands.ContextMenu(
            name="get_message_history",
            callback=self.get_message_history,
        )

    async def cog_load(self) -> None:
        await self.db.initialize(datetime.now(JST))
        self.bot.tree.add_command(self.message_history_menu)
        self.survival_confirmation_loop.start()

    async def cog_unload(self) -> None:
        self.survival_confirmation_loop.cancel()
        self.bot.tree.remove_command(self.message_history_menu.name, type=self.message_history_menu.type)

    def _require_admin(self, interaction: Interaction) -> None:
        if isinstance(interaction.user, Member) and any(role.id == self.admin_role_id for role in interaction.user.roles):
            return
        error_message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(error_message)

    @commands.Cog.listener("on_message")
    async def insert_new_message(self, message: Message) -> None:
        if message.flags.ephemeral or not isinstance(message.channel, (TextChannel, Thread)):
            return
        async with self.db.session() as session:
            await insert_message(session, message)

    @commands.Cog.listener("on_message_delete")
    async def delete_message(self, message: Message) -> None:
        async with self.db.session() as session:
            if previous := await _latest_message(session, message.id):
                previous.status = 1
                await session.commit()

    @commands.Cog.listener("on_raw_message_edit")
    async def update_message(self, payload: RawMessageUpdateEvent) -> None:
        channel = await self.bot.fetch_channel(payload.channel_id)
        if not isinstance(channel, (TextChannel, Thread)):
            return
        message = await channel.fetch_message(payload.message_id)
        async with self.db.session() as session:
            await insert_message(session, message)

    @app_commands.command(description="サーバーのメンバー一覧をTalkDataへ登録します。")
    async def upsert_member(self, interaction: Interaction) -> None:
        self._require_admin(interaction)
        if interaction.guild is None:
            error_message = "Guild内で実行してください。"
            raise TypeError(error_message)
        success: list[str] = []
        errors: list[str] = []
        async with self.db.session() as session:
            for member in interaction.guild.members:
                try:
                    await self.db.upsert_member(session, member.id, member.display_name)
                    await session.commit()
                except SQLAlchemyError:
                    await session.rollback()
                    logger.exception("Failed to add member: %s", member.id)
                    errors.append(member.display_name)
                else:
                    success.append(member.display_name)
        await interaction.response.send_message(
            format_upsert_result(success, errors, "メンバー"),
            ephemeral=True,
        )

    @app_commands.command(description="サーバーのチャンネル一覧をTalkDataへ登録します。")
    async def upsert_channel(self, interaction: Interaction) -> None:
        self._require_admin(interaction)
        if interaction.guild is None:
            error_message = "Guild内で実行してください。"
            raise TypeError(error_message)
        success: list[str] = []
        errors: list[str] = []
        async with self.db.session() as session:
            channels = await interaction.guild.fetch_channels()
            for channel in channels:
                if not isinstance(channel, (TextChannel, ForumChannel)):
                    continue
                records = [(channel.id, channel.name, 0), *((thread.id, thread.name, channel.id) for thread in channel.threads)]
                for channel_id, name, parent_id in records:
                    try:
                        await self.db.upsert_channel(session, channel_id, name, parent_id)
                        await session.commit()
                    except SQLAlchemyError:
                        await session.rollback()
                        logger.exception("Failed to add channel: %s", channel_id)
                        errors.append(name)
                    else:
                        success.append(name)
        await interaction.response.send_message(
            format_upsert_result(success, errors, "チャンネル"),
            ephemeral=True,
        )

    @app_commands.command(description="現在のチャンネルの全メッセージをTalkDataへ登録します。")
    async def insert_messages_in_this_channel(self, interaction: Interaction) -> None:
        self._require_admin(interaction)
        channel = _require_message_channel(interaction.channel)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.db.session() as session:
            await insert_channel_history(session, channel)
        await interaction.followup.send("メッセージの登録が完了しました。", ephemeral=True)

    @app_commands.command(description="全テキストチャンネルとスレッドのメッセージをTalkDataへ登録します。")
    async def insert_all_messages(self, interaction: Interaction) -> None:
        self._require_admin(interaction)
        if interaction.guild is None:
            error_message = "Guild内で実行してください。"
            raise TypeError(error_message)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.db.session() as session:
            for channel in await interaction.guild.fetch_channels():
                if isinstance(channel, TextChannel):
                    await insert_channel_history(session, channel)
                if isinstance(channel, (TextChannel, ForumChannel)):
                    for thread in channel.threads:
                        await insert_channel_history(session, thread)
        await interaction.followup.send("全メッセージの登録が完了しました。", ephemeral=True)

    async def get_message_history(self, interaction: Interaction, message: Message) -> None:
        if interaction.guild is None:
            error_message = "Guild内で実行してください。"
            raise TypeError(error_message)
        async with self.db.session() as session:
            messages = (
                await session.scalars(
                    select(DiscordMessage).where(DiscordMessage.id == message.id).order_by(DiscordMessage.edit_count)
                )
            ).all()
            author = await session.get(DiscordMember, message.author.id)
            if author is None:
                error_message = f"対応するメンバーがDB内に見つかりませんでした…💦: {message.author.id}"
                raise TalkDataNotFoundError(error_message)
            body = [f"[このメッセージ](<{message.jump_url}>)の編集履歴を表示します:", author.display_name]
            for previous in messages:
                body.extend(await self._format_message(session, interaction.guild.id, previous))
        await interaction.response.send_message("\n".join(body))

    @app_commands.command(description="指定メンバーの削除・編集済みメッセージを表示します。")
    @app_commands.describe(member="対象メンバー", start="開始時刻 YYYY-MM-DD HH-MM", end="終了時刻 YYYY-MM-DD HH-MM")
    async def get_depreciated_message(
        self,
        interaction: Interaction,
        member: Member,
        start: str | None = None,
        end: str | None = None,
    ) -> None:
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}$")
        if (start and not pattern.fullmatch(start)) or (end and not pattern.fullmatch(end)):
            await interaction.response.send_message("時刻は `YYYY-MM-DD HH-MM` の形式で指定してください。", ephemeral=True)
            return
        start_at = (
            datetime.strptime(start, "%Y-%m-%d %H-%M").replace(tzinfo=JST) if start else datetime.now(JST) - timedelta(hours=1)
        )
        end_at = datetime.strptime(end, "%Y-%m-%d %H-%M").replace(tzinfo=JST) if end else datetime.now(JST)
        if start_at > end_at:
            await interaction.response.send_message("開始時刻は終了時刻よりも前に設定してください。", ephemeral=True)
            return
        if interaction.channel_id is None or interaction.guild_id is None:
            error_message = "Guild内のチャンネルで実行してください。"
            raise TypeError(error_message)
        async with self.db.session() as session:
            messages = (
                await session.scalars(
                    select(DiscordMessage)
                    .where(
                        DiscordMessage.member_id == member.id,
                        DiscordMessage.channel_id == interaction.channel_id,
                        DiscordMessage.post_time.between(start_at, end_at),
                        DiscordMessage.status >= 1,
                    )
                    .order_by(DiscordMessage.post_time)
                )
            ).all()
            if not messages:
                await interaction.response.send_message("該当するメッセージが見つかりませんでした。", ephemeral=True)
                return
            body = [
                f"{start_at:%Y-%m-%d %H:%M}から{end_at:%Y-%m-%d %H:%M}の間にこのチャンネルで削除された投稿を表示します:",
                member.display_name,
            ]
            for message in messages:
                body.extend(await self._format_message(session, interaction.guild_id, message))
                if message.status == DELETED_STATUS:
                    body.append("(削除済)")
                elif message.status == EDITED_STATUS:
                    url = f"https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{message.id}"
                    body.append(f"[(編集済)](<{url}>)")
        await interaction.response.send_message("\n".join(body))

    async def _format_message(self, session: AsyncSession, guild_id: int, message: DiscordMessage) -> list[str]:
        lines = [message.post_time.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")]
        if message.reply_id:
            reply = await session.get(DiscordMessage, (message.reply_id, message.reply_edit_count))
            if reply is None:
                error_message = f"返信元メッセージがDB内に見つかりませんでした: {message.reply_id}"
                raise TalkDataNotFoundError(error_message)
            log = await self.db.get_message_log(session, reply.id, reply.edit_count)
            summary = log.content[:REPLY_SUMMARY_LENGTH].replace("\n", "")
            summary += "…" if len(log.content) > REPLY_SUMMARY_LENGTH else ""
            url = f"https://discord.com/channels/{guild_id}/{log.channel_id}/{reply.id}"
            lines.append(f"> [***in reply to @{log.name}:*** {summary}](<{url}>)")
        lines.append(message.content)
        lines.extend(f"<{url}>" for url in message.attachment.split(",") if url)
        lines.append("")
        return lines

    @tasks.loop(minutes=1)
    async def survival_confirmation_loop(self) -> None:
        if await self.db.connection_is_available():
            self.failed_connection_checks = 0
            return
        self.failed_connection_checks += 1
        logger.error("Cannot connect to TalkData DB (%s/5)", self.failed_connection_checks)
        if self.failed_connection_checks >= MAX_FAILED_CONNECTION_CHECKS:
            logger.critical("TalkData DBへの接続が5回連続で失敗しました。")
            self.survival_confirmation_loop.stop()


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(TalkData(bot, session_factory))
