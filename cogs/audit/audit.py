import os
from datetime import timedelta, timezone
from enum import Enum
from logging import getLogger
from typing import Any

import discord
from discord import Message, TextChannel, Thread
from discord.ext import commands

from core.runtime_environment import get_runtime_environment
from core.tools.utils import parse_comma_separated_values
from core.tools.webhook import fetch_webhook

logger = getLogger(__name__)
WEBHOOK_NAME = "auditor"
JST = timezone(timedelta(hours=9))
REPLY_PREVIEW_LENGTH = 20


class Event(Enum):
    edit = "edit"
    delete = "delete"


async def _get_or_fetch_member(guild: discord.Guild, user_id: int) -> discord.Member:
    """ギルド内メンバーをキャッシュから取得し、なければAPIで取得する。存在しない場合は例外を送出する。"""
    if member := guild.get_member(user_id):
        return member
    return await guild.fetch_member(user_id)


async def _get_or_fetch_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel | discord.Thread:
    """ギルド内チャンネル/スレッドをキャッシュから取得し、なければAPIで取得する。

    THREAD_ID_LOG はスレッドを想定しているが、親が TextChannel の場合もあるため
    TextChannel | Thread に限定して返す。該当しない種別の場合は例外を送出する。
    """
    channel = guild.get_channel(channel_id)
    if isinstance(channel, (TextChannel, Thread)):
        return channel

    fetched = await guild.fetch_channel(channel_id)
    if isinstance(fetched, (TextChannel, Thread)):
        return fetched

    error_message = f"channel {channel_id} is not a text channel or thread"
    raise TypeError(error_message)


async def _create_reply_prefix(message: Message) -> str:
    """返信元へのリンクを含む監査ログ用のプレフィックスを作成する。"""
    if message.reference is None or message.reference.message_id is None:
        return ""

    try:
        ref = await message.channel.fetch_message(message.reference.message_id)
    except discord.NotFound:
        if isinstance(message.channel, Thread) and isinstance(message.channel.parent, TextChannel):
            ref = await message.channel.parent.fetch_message(message.reference.message_id)
        else:
            raise

    # URLを含む文字列にリンクを付与できないのでURLは無効化している
    reply_content = ref.clean_content[:REPLY_PREVIEW_LENGTH]
    reply_content = reply_content.replace("\n", "").replace("http:/", "http: /").replace("https:/", "https: /")
    ellipsis = "…" if len(ref.clean_content) > REPLY_PREVIEW_LENGTH else ""
    return f"> [***in reply to @{ref.author.display_name}:*** {reply_content}{ellipsis}]({ref.jump_url})\n"


async def create_webhook_log_message(message: Message, event: Event) -> dict[str, Any]:
    """渡した`message`をベースとして、編集・削除履歴のログを送信する`Webhook`の要素を作成する。"""
    content = await _create_reply_prefix(message) + message.content

    if message.stickers:  # スタンプに関する処理
        content += message.stickers[0].url

    if interaction_metadata := message.interaction_metadata:  # インタラクションに関する処理
        user = interaction_metadata.user
        try:
            if message.guild:
                user = await _get_or_fetch_member(message.guild, user.id)
        except discord.HTTPException:
            user = interaction_metadata.user

        # interaction_metadata にはコマンド名が含まれないため、非推奨APIを参照せず種別のみ記録する。
        content = f"> ***@{user.display_name}** invoked an interaction:*\n" + content

    match event:
        case Event.edit:
            content += "\n[***(edited; "
        case Event.delete:
            content += "\n[***(deleted; "

    if message.edited_at:
        content += "originally edited at {})***]({})".format(
            message.edited_at.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S"),
            message.jump_url,
        )
    else:
        content += "originally posted at {})***]({})".format(
            message.created_at.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S"),
            message.jump_url,
        )

    return {
        "content": content,
        # 監査対象は TextChannel/Thread のみだが、Message 型には name がないため型チェックを抑制する
        "username": message.author.display_name + " on #" + message.channel.name,  # type: ignore[union-attr]
        "avatar_url": message.author.display_avatar.url,
        "files": [(await atch.to_file()) for atch in message.attachments],
        "embeds": message.embeds,
        "allowed_mentions": discord.AllowedMentions.none(),
    }


class Audit(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot
        self.runtime_environment = get_runtime_environment()
        self.hook: discord.Webhook | None = None
        self.server_id: int = int(os.environ["SERVER_ID"])
        self.log_thread: int = int(os.environ["THREAD_ID_LOG"])
        self.audit_immunity = [int(channel_id) for channel_id in parse_comma_separated_values(os.environ.get("AUDIT_IMMUNITY"))]

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.runtime_environment.is_debug:
            logger.info("Disabled audit log forwarding in debug environment")
            return
        thread = await self.bot.fetch_channel(self.log_thread)
        if not isinstance(thread, (TextChannel, Thread)):
            error_message = f"audit log channel {self.log_thread} is not a text channel or thread"
            raise TypeError(error_message)
        self.hook = await fetch_webhook(thread, name=WEBHOOK_NAME)

    async def _get_hook(self, thread: TextChannel | Thread) -> discord.Webhook:
        """キャッシュ済みの送信用Webhookを返し、未取得時だけ作成する。"""
        if self.hook is None:
            self.hook = await fetch_webhook(thread, name=WEBHOOK_NAME)
        return self.hook

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        # 監査ログ用Webhookからのメッセージは除外するが、現状は後続処理がない (互換維持のため残置)
        if message.author.bot or (self.hook is not None and message.webhook_id == self.hook.id):
            return

    @commands.Cog.listener("on_message_delete")
    async def message_delete_log(self, message: Message) -> None:
        if self.runtime_environment.is_debug:
            return
        if not message.guild or message.guild.id != self.server_id:
            return

        # 特定のチャンネルでの活動はaudit-logにメッセージを追記しない
        # 同等の処理は on_message_edit にも用意する必要あり

        if message.channel.id in self.audit_immunity or (
            isinstance(message.channel, Thread) and message.channel.parent_id in self.audit_immunity
        ):
            return

        if self.hook is not None and message.webhook_id == self.hook.id and message.channel.id == self.log_thread:
            thread = await _get_or_fetch_channel(message.guild, self.log_thread)
            await thread.send("⚠️ 監査ログを削除しないでください")

            self.hook = None
            self.hook = await fetch_webhook(thread, name=WEBHOOK_NAME)
            await self.hook.send(
                content=message.content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                files=[(await atch.to_file()) for atch in message.attachments],
                embeds=message.embeds,
                allowed_mentions=discord.AllowedMentions.none(),
                thread=thread,
            )

        elif isinstance(message.channel, (TextChannel, Thread)):
            thread = await _get_or_fetch_channel(message.guild, self.log_thread)
            hook = await self._get_hook(thread)

            await hook.send(
                **(await create_webhook_log_message(message, Event.delete)),
                thread=thread,
            )

    @commands.Cog.listener("on_message_edit")
    async def message_edit_log(self, before: Message, after: Message) -> None:
        if self.runtime_environment.is_debug:
            return
        if not after.guild or after.guild.id != self.server_id:
            return

        if before.channel.id in self.audit_immunity or (
            isinstance(before.channel, Thread) and before.channel.parent_id in self.audit_immunity
        ):
            return

        if (before.content == after.content and before.attachments == []) or after.author.bot:
            return

        if isinstance(before.channel, (TextChannel, Thread)):
            thread = await _get_or_fetch_channel(before.guild, self.log_thread)
            hook = await self._get_hook(thread)

            await hook.send(
                **(await create_webhook_log_message(before, Event.edit)),
                thread=thread,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Audit(bot))
    logger.debug("%s is added to the bot.", __name__)
