import asyncio
import os
import re
from datetime import timedelta, timezone
from logging import getLogger
from typing import Any

import discord
from discord import ForumChannel, Interaction, Member, Message, TextChannel, Thread, Webhook, app_commands
from discord.ext import commands

from core.exception.exception import ArgumentError, MissingRequiredRoleError
from core.tools.webhook import fetch_webhook

WEBHOOK_NAME = "papyrus-moving"
MESSAGE_URL_PATTERN = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/\d+/\d+/(\d+)(?:\?.*)?$")
REPLY_PREVIEW_LENGTH = 20
SEND_INTERVAL_SECONDS = 1.5
JST = timezone(timedelta(hours=9))
logger = getLogger(__name__)


def fetch_message_id_from_url(url: str) -> int:
    """DiscordメッセージURLからメッセージIDを取り出す。"""
    match = MESSAGE_URL_PATTERN.search(url)
    if match is None:
        error_message = "DiscordのメッセージURLを指定してください…💦"
        raise ArgumentError(error_message)
    return int(match.group(1))


def _require_message_channel(channel: object) -> TextChannel | Thread:
    """移動元が履歴を取得できるテキストチャンネルまたはスレッドか確認する。"""
    if isinstance(channel, TextChannel):
        return channel
    if isinstance(channel, Thread) and isinstance(channel.parent, (TextChannel, ForumChannel)):
        return channel

    error_message = "ここはテキストチャンネルでもスレッドでもありません…💦"
    raise ArgumentError(error_message)


async def thread_overlap_detection(channel: TextChannel, thread_name: str) -> None:
    """同名のアクティブなスレッドがある場合は作成を中断する。"""
    if any(thread.name == thread_name for thread in channel.threads):
        error_message = "既存のスレッドと同名のスレッドは作成できません…💦"
        raise ArgumentError(error_message)


async def _reply_prefix(message: Message, url_map: dict[int, str]) -> str:
    """移動先または移動元の返信先へリンクするプレフィックスを作る。"""
    if message.reference is None or message.reference.message_id is None:
        return ""

    try:
        referenced = await message.channel.fetch_message(message.reference.message_id)
    except discord.NotFound:
        if isinstance(message.channel, Thread) and isinstance(message.channel.parent, TextChannel):
            referenced = await message.channel.parent.fetch_message(message.reference.message_id)
        else:
            raise

    url = url_map.get(referenced.id, referenced.jump_url)
    preview = referenced.clean_content[:REPLY_PREVIEW_LENGTH].replace("\n", "")
    ellipsis = "…" if len(referenced.clean_content) > REPLY_PREVIEW_LENGTH else ""
    return f"> [***in reply to @{referenced.author.display_name}:*** {preview}{ellipsis}](<{url}>)\n"


async def create_webhook_message(message: Message, url_map: dict[int, str]) -> dict[str, Any]:
    """元メッセージを変更せず、Webhook送信用の要素を作成する。"""
    content = await _reply_prefix(message, url_map) + message.content

    if message.stickers:
        content += str(message.stickers[0].url)

    if interaction_metadata := message.interaction_metadata:
        user = interaction_metadata.user
        if message.guild is not None:
            member = message.guild.get_member(user.id)
            if member is None:
                try:
                    member = await message.guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if member is not None:
                user = member
        content = f"> ***@{user.display_name}** invoked an interaction:*\n" + content

    if message.edited_at is not None:
        content += " *(edited)*"

    return {
        "content": content,
        "username": message.author.display_name + message.created_at.astimezone(JST).strftime(" (%Y/%m/%d %H:%M)"),
        "avatar_url": message.author.display_avatar.url,
        "files": [await attachment.to_file() for attachment in message.attachments],
        "embeds": message.embeds,
        "allowed_mentions": discord.AllowedMentions.none(),
    }


async def send_copy_via_webhook(
    hook: Webhook,
    destination: Thread | TextChannel,
    message: Message,
    url_map: dict[int, str],
    interval: float = SEND_INTERVAL_SECONDS,
) -> None:
    """メッセージをWebhookで複製し、元IDと複製先URLの対応を更新する。"""
    try:
        sent_message = await hook.send(
            **(await create_webhook_message(message, url_map)),
            thread=destination if isinstance(destination, Thread) else discord.utils.MISSING,
            wait=True,
        )
    except discord.HTTPException:
        logger.exception("Failed to copy message via webhook: message_id=%s", message.id)
    else:
        if sent_message is not None:
            url_map[message.id] = sent_message.jump_url

    await asyncio.sleep(interval)


async def transport_messages(
    hook: Webhook,
    original: TextChannel | Thread,
    destination: Thread | TextChannel,
    *,
    exclude_last: bool = True,
) -> None:
    """移動元の全メッセージを古い順に複製する。"""
    url_map: dict[int, str] = {}
    last_id = original.last_message_id if exclude_last else None
    is_first = True

    async for message in original.history(limit=None, oldest_first=True):
        if is_first:
            await _copy_parent_reference(hook, original, destination, message, url_map)
            is_first = False

        if message.id != last_id:
            await send_copy_via_webhook(hook, destination, message, url_map)


async def _copy_parent_reference(
    hook: Webhook,
    original: TextChannel | Thread,
    destination: Thread | TextChannel,
    first_message: Message,
    url_map: dict[int, str],
) -> None:
    """スレッド先頭メッセージが親チャンネルへ返信している場合、その返信先も複製する。"""
    if not isinstance(original, Thread) or not isinstance(original.parent, TextChannel):
        return
    if first_message.reference is None or first_message.reference.message_id is None:
        return

    try:
        referenced = await original.parent.fetch_message(first_message.reference.message_id)
    except discord.HTTPException:
        return
    await send_copy_via_webhook(hook, destination, referenced, url_map)


async def transport_messages_in_specified_period(
    hook: Webhook,
    original: TextChannel | Thread,
    destination: Thread | TextChannel,
    start: Message,
    last: Message,
) -> None:
    """指定された先頭・末尾メッセージを含む期間を古い順に複製する。"""
    url_map: dict[int, str] = {}
    after = discord.Object(id=start.id - 1)
    before = discord.Object(id=last.id + 1)

    async for message in original.history(limit=None, oldest_first=True, after=after, before=before):
        await send_copy_via_webhook(hook, destination, message, url_map)


class Moving(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.admin_role_id = int(os.environ["ROLE_ID_BOT_ADMIN"])

    async def interaction_check(self, interaction: Interaction) -> bool:
        """全コマンドの実行者が管理者ロールを持つか確認する。"""
        if isinstance(interaction.user, Member) and any(role.id == self.admin_role_id for role in interaction.user.roles):
            return True
        error_message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(error_message)

    @app_commands.command(description="現在のチャンネルまたはスレッドを新しいスレッドへ複製します。")
    @app_commands.describe(channel="新規スレッドを作成するチャンネル", thread_name="新規スレッドの名称")
    async def duplicate_thread(self, interaction: Interaction, channel: TextChannel, thread_name: str) -> None:
        original = _require_message_channel(interaction.channel)
        await interaction.response.defer(thinking=True)
        await thread_overlap_detection(channel, thread_name)

        destination = await channel.create_thread(name=thread_name, type=discord.ChannelType.public_thread)
        hook = await fetch_webhook(destination, name=WEBHOOK_NAME)
        source_name = "チャンネル" if isinstance(original, TextChannel) else "スレッド"
        await destination.send(f"{source_name} {original.mention} を複製します…")
        await transport_messages(hook, original, destination)
        await destination.send(f"{source_name} {original.mention} を正常に複製しました 👌")
        await interaction.followup.send("お引越し終了! うまくできたかな…?")

    @app_commands.command(description="現在のチャンネルまたはスレッドの全メッセージを移動先へ追記します。")
    @app_commands.describe(destination="移動先のチャンネルまたはスレッド")
    async def postscript_thread(self, interaction: Interaction, destination: TextChannel | Thread) -> None:
        original = _require_message_channel(interaction.channel)
        await interaction.response.defer(thinking=True)
        hook = await fetch_webhook(destination, name=WEBHOOK_NAME)
        source_name = "チャンネル" if isinstance(original, TextChannel) else "スレッド"
        await destination.send(f"{source_name} {original.mention} のメッセージを追加します…")
        await transport_messages(hook, original, destination)
        await destination.send(f"{source_name} {original.mention} のメッセージを正常に追加しました 👌")
        await interaction.followup.send("追記終了! うまくできたかな…?")

    @app_commands.command(description="指定範囲のメッセージを移動先へ追記します。")
    @app_commands.describe(
        destination="移動先のチャンネルまたはスレッド",
        start_url="先頭メッセージのURL",
        last_url="末尾メッセージのURL",
    )
    async def postscript_thread_two(
        self,
        interaction: Interaction,
        destination: TextChannel | Thread,
        start_url: str,
        last_url: str,
    ) -> None:
        original = _require_message_channel(interaction.channel)
        await interaction.response.defer(thinking=True)
        start = await self._fetch_boundary_message(original, start_url)
        last = await self._fetch_boundary_message(original, last_url)
        if start.id > last.id:
            error_message = "指定された `last_url` は `start_url` より先に送信されたメッセージです…💦"
            raise ArgumentError(error_message)

        hook = await fetch_webhook(destination, name=WEBHOOK_NAME)
        source_name = "チャンネル" if isinstance(original, TextChannel) else "スレッド"
        await destination.send(f"{source_name} {original.mention} のメッセージの指定部分を追加します…")
        await transport_messages_in_specified_period(hook, original, destination, start, last)
        await destination.send(f"{source_name} {original.mention} のメッセージの指定部分を正常に追加しました 👌")
        await interaction.followup.send("追記終了! うまくできたかな…?")

    @staticmethod
    async def _fetch_boundary_message(channel: TextChannel | Thread, url: str) -> Message:
        """URLが示す境界メッセージを現在のチャンネルから取得する。"""
        try:
            return await channel.fetch_message(fetch_message_id_from_url(url))
        except discord.HTTPException as error:
            error_message = f"対応するメッセージが見つかりませんでした…💦: url={url}"
            raise ArgumentError(error_message) from error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moving(bot))
