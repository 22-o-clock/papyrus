import asyncio
import os
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from logging import getLogger

import discord
import regex
from discord import Interaction, Member, Message, RawReactionActionEvent, TextChannel, Thread, app_commands
from discord.ext import commands, tasks

from core.exception.exception import ArgumentError, MissingRequiredRoleError
from core.runtime_environment import get_runtime_environment
from core.tools.ebd import add_timestamp_footer, make_simple_embed
from core.tools.utils import fetch_text_channel

from .normalization import FuzzyMatch

logger = getLogger(__name__)

JST = timezone(timedelta(hours=9))
URL_PATTERN = re.compile(r"https?://[\w/:%#$&?()~.=+\-]+")
EMOJI_PATTERN = regex.compile(r"^[☹-☻🌑-🌠🎀-🎗🐀-👅👤-💈💋-💒🕷 🕸🕺😀-🙏🤐-🤗🤠-🤷🥰-🥺🦀-🦭🦰-🦳🦸 🦹🧌-🧟🫂-🫅🫠-🫥]$")
MAX_CACHE_SIZE = 10
MAX_REMOVAL_LOG_MESSAGES = 20
MAX_EMBED_FIELD_LENGTH = 1024


@dataclass(slots=True)
class AddressedMessage:
    id: int
    content: str = "[already addressed]"


CachedMessage = AddressedMessage | Message


def _extract_string(value: object) -> str:
    return value if isinstance(value, str) else ""


class GateKeeper:
    """ユーザー・チャンネル別の直近メッセージを使って禁止表現を検出する。"""

    def __init__(self, pattern: str = r"$^") -> None:
        self.cache_by_channel: dict[int, dict[int, list[CachedMessage]]] = {}
        self.checker = FuzzyMatch(pattern)

    def check(self, message: Message, *, on_delete: bool, non_delete: bool) -> list[Message]:
        """検出対象となったメッセージを返し、キャッシュ状態を更新する。"""
        result: list[Message] = []
        if not on_delete and self.message_matches(message):
            result = [message]
        elif concatenated := self.concatenated_matches(message):
            result = concatenated
        elif vertical := self.vertical_matches(message):
            result = vertical

        for detected in result:
            if non_delete:
                self.exclude(detected)
            else:
                self.delete(detected)
        return result

    def get_cache(self, message: Message) -> list[CachedMessage]:
        """同じチャンネル・投稿者の直近メッセージを返す。"""
        return self.cache_by_channel.get(message.channel.id, {}).get(message.author.id, [])

    def message_matches(self, message: Message) -> bool:
        """本文、添付名、Embed内の文字列を検査する。"""
        if self.checker.is_match(message.content):
            return True
        if any(
            self.checker.is_match(attachment.filename + _extract_string(attachment.description))
            for attachment in message.attachments
        ):
            return True

        for embed in message.embeds:
            text = (
                _extract_string(embed.author.name)
                + _extract_string(embed.title)
                + _extract_string(embed.description)
                + _extract_string(embed.footer.text)
            )
            if self.checker.is_match(text):
                return True
            if any(self.checker.is_match(field.name + field.value) for field in embed.fields):
                return True
        return False

    def concatenated_matches(self, message: Message) -> list[Message]:
        """分割投稿を連結した結果が禁止表現になる場合、その投稿群を返す。"""
        cache = self.get_cache(message)
        if not cache or not self.checker.is_match("".join(cached.content for cached in cache)):
            return []

        text = ""
        detected: list[Message] = []
        for cached in reversed(cache):
            if isinstance(cached, AddressedMessage):
                break
            text = cached.content + text
            detected.insert(0, cached)
            if self.checker.is_match(text):
                return detected
        return []

    def vertical_matches(self, message: Message) -> list[Message]:
        """複数投稿の行頭を連結した縦読みが禁止表現になる場合、その投稿群を返す。"""
        cache = self.get_cache(message)
        if not cache:
            return []

        initials = "".join(sentence[0] for cached in cache for sentence in cached.content.split() if sentence)
        if not self.checker.is_match(initials):
            return []
        return [cached for cached in cache if isinstance(cached, Message)]

    def add(self, message: Message) -> None:
        """新しいメッセージを投稿者別キャッシュへ追加する。"""
        author_cache = self.cache_by_channel.setdefault(message.channel.id, {}).setdefault(message.author.id, [])
        author_cache.append(message)
        if len(author_cache) > MAX_CACHE_SIZE:
            author_cache.pop(0)

    def edit(self, message: Message) -> None:
        """編集されたメッセージがキャッシュ内にあれば更新する。"""
        self._replace(message, message)

    def delete(self, message: Message) -> None:
        """削除されたメッセージをキャッシュから除去する。"""
        cache = self.get_cache(message)
        cache[:] = [cached for cached in cache if cached.id != message.id]

    def exclude(self, message: Message) -> None:
        """対処済みメッセージを連結検知の境界へ置換する。"""
        self._replace(message, AddressedMessage(message.id))

    def _replace(self, message: Message, replacement: CachedMessage) -> None:
        cache = self.get_cache(message)
        for index, cached in enumerate(cache):
            if cached.id == message.id:
                cache[index] = replacement
                return


class Monitor(commands.Cog):
    moderation = app_commands.Group(name="moderation", description="投稿とリアクションの制限を管理します。")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.runtime_environment = get_runtime_environment()
        self.server_id = int(os.environ["SERVER_ID"])
        self.admin_role_id = int(os.environ["ROLE_ID_BOT_ADMIN"])
        self.log_thread_id = int(os.environ["THREAD_ID_ANTHYME_LOG"])
        self.gatekeeper = GateKeeper("so[uー]*nan+da")
        self.non_delete_mode = True
        self.banned_users: set[int] = set()
        self.reaction_banned_users: set[int] = set()
        self.removed_messages: list[Message] = []

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.removal_logger.is_running():
            self.removal_logger.start()

    async def cog_unload(self) -> None:
        self.removal_logger.cancel()

    async def censor(self, message: Message, *, non_delete: bool = True) -> None:
        """違反メッセージへリアクションするか、短い猶予後に削除する。"""
        if non_delete:
            await message.add_reaction("👎")
            await message.add_reaction("💔")
            if cry_emoji := discord.utils.get(self.bot.emojis, name="miku_cry"):
                await message.add_reaction(cry_emoji)
            return

        try:
            await message.add_reaction("👎")
        except discord.Forbidden:
            await message.delete(delay=0.5)
        else:
            await message.delete(delay=2)
        self.removed_messages.append(message)

    async def surveillance(self, message: Message, *, on_delete: bool = False) -> None:
        """対象サーバーのメッセージへ設定中の監視処理を適用する。"""
        if message.guild is None or message.guild.id != self.server_id:
            return

        if not message.author.bot and self.runtime_environment.is_production:
            for detected in self.gatekeeper.check(message, on_delete=on_delete, non_delete=self.non_delete_mode):
                await self.censor(detected, non_delete=self.non_delete_mode)

        if (
            not message.author.bot
            and not on_delete
            and message.author.id in self.reaction_banned_users
            and EMOJI_PATTERN.search(message.content)
        ):
            await self.censor(message, non_delete=False)
            return

        if (
            not on_delete
            and message.author.id in self.banned_users
            and (URL_PATTERN.search(message.content) or message.attachments)
        ):
            await self.censor(message, non_delete=False)

    @tasks.loop(minutes=5)
    async def removal_logger(self) -> None:
        if not self.removed_messages:
            return

        messages = self.removed_messages[:MAX_REMOVAL_LOG_MESSAGES]
        embed = make_simple_embed(discord.Colour.light_grey(), "以下のメッセージを自動的に削除しました (最大20個まで表示) 📝")
        for message in messages:
            channel_name = getattr(message.channel, "name", str(message.channel.id))
            author_name = (
                message.author.nick
                if isinstance(message.author, Member) and message.author.nick
                else message.author.display_name
            )
            embed.add_field(
                name=f"by {author_name} on #{channel_name} ({message.created_at.astimezone(JST):%Y-%m-%d %H:%M})",
                value=message.clean_content[:MAX_EMBED_FIELD_LENGTH] or "(本文なし)",
                inline=False,
            )

        self.removed_messages = self.removed_messages[len(messages) :]
        add_timestamp_footer(self.bot, embed)
        channel = await fetch_text_channel(self.bot, self.log_thread_id)
        await channel.send(embed=embed)

    @removal_logger.before_loop
    async def before_removal_logger(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        self.gatekeeper.add(message)
        await self.surveillance(message)

    @commands.Cog.listener()
    async def on_message_edit(self, _: Message, after: Message) -> None:
        self.gatekeeper.edit(after)
        await self.surveillance(after)

    @commands.Cog.listener()
    async def on_message_delete(self, message: Message) -> None:
        self.gatekeeper.delete(message)
        await self.surveillance(message, on_delete=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        guild_id = payload.guild_id
        if guild_id is None or guild_id != self.server_id or payload.user_id not in self.reaction_banned_users:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(payload.channel_id)
        if isinstance(channel, (TextChannel, Thread)):
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, member)

    @moderation.command(name="reaction_ban", description="指定ユーザーのリアクションを制限します。")
    async def reaction_ban(self, interaction: Interaction, user: Member) -> None:
        if user.id in self.reaction_banned_users:
            await interaction.response.send_message(f"{user.mention} のリアクションはすでに制限されています...", ephemeral=True)
            return
        self.reaction_banned_users.add(user.id)
        await interaction.response.send_message(f"{user.mention} のリアクションを制限します...")

    @moderation.command(name="reaction_unban", description="指定ユーザーのリアクション制限を解除します。")
    async def reaction_unban(self, interaction: Interaction, user: Member) -> None:
        if user.id not in self.reaction_banned_users:
            await interaction.response.send_message(f"{user.mention} のリアクションは制限されていません...", ephemeral=True)
            return
        self.reaction_banned_users.discard(user.id)
        await interaction.response.send_message(f"{user.mention} のリアクション制限を解除します...")

    @moderation.command(name="reaction_remove", description="直近の自分の投稿に付いたリアクションを削除します。")
    async def remove_reactions(self, interaction: Interaction, user: Member | None = None) -> None:
        channel = self._require_text_channel(interaction.channel)
        await interaction.response.defer(thinking=True)
        await self._remove_reactions_from_history(channel, interaction.user.id, user)
        await interaction.followup.send("指定されたリアクションを削除しました 👌")

    @moderation.command(name="reaction_remove_bot", description="直近のボットの投稿に付いたリアクションを削除します。")
    async def remove_my_reactions(self, interaction: Interaction, user: Member | None = None) -> None:
        channel = self._require_text_channel(interaction.channel)
        if self.bot.user is None:
            error_message = "Bot user is not ready"
            raise RuntimeError(error_message)
        await interaction.response.defer(thinking=True)
        await self._remove_reactions_from_history(channel, self.bot.user.id, user)
        await interaction.followup.send("指定されたリアクションを削除しました 👌")

    async def _remove_reactions_from_history(self, channel: TextChannel | Thread, author_id: int, user: Member | None) -> None:
        """指定投稿者の直近投稿からリアクションを削除する。"""
        operations = []
        async for message in channel.history(limit=250):
            if message.author.id != author_id or not message.reactions:
                continue
            if user is None:
                operations.append(message.clear_reactions())
            else:
                operations.append(self._remove_user_reaction(message, user))
        await asyncio.gather(*operations)

    @staticmethod
    async def _remove_user_reaction(message: Message, user: Member) -> None:
        """指定ユーザーが付けたリアクションだけをメッセージから削除する。"""
        operations = []
        for reaction in message.reactions:
            operations.extend(
                [reaction.remove(reacted_user) async for reacted_user in reaction.users() if reacted_user.id == user.id]
            )
        await asyncio.gather(*operations)

    @moderation.command(name="post_ban", description="指定ユーザーのURL・添付投稿を制限します。")
    async def ban(self, interaction: Interaction, user: Member) -> None:
        self._require_admin(interaction)
        if user.id in self.banned_users:
            await interaction.response.send_message(f"{user.mention} の投稿はすでに制限されています...", ephemeral=True)
            return
        self.banned_users.add(user.id)
        await interaction.response.send_message(f"{user.mention} の一部の投稿を制限します...")

    @moderation.command(name="post_unban", description="指定ユーザーの投稿制限を解除します。")
    async def unban(self, interaction: Interaction, user: Member) -> None:
        self._require_admin(interaction)
        if user.id not in self.banned_users:
            await interaction.response.send_message(f"{user.mention} の投稿は制限されていません...", ephemeral=True)
            return
        self.banned_users.discard(user.id)
        await interaction.response.send_message(f"{user.mention} の投稿制限を解除します...")

    @moderation.command(name="expression_config", description="禁止表現への対処方法を切り替えます。")
    async def global_ban_config(self, interaction: Interaction) -> None:
        self._require_admin(interaction)
        self.non_delete_mode = not self.non_delete_mode
        message = "メッセージの自動削除を終了します..." if self.non_delete_mode else "メッセージの自動削除を開始します..."
        await interaction.response.send_message(message)

    def _require_admin(self, interaction: Interaction) -> None:
        """実行者が管理者ロールを持たなければコマンドを中断する。"""
        if isinstance(interaction.user, Member) and any(role.id == self.admin_role_id for role in interaction.user.roles):
            return
        error_message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(error_message)

    @staticmethod
    def _require_text_channel(channel: object) -> TextChannel | Thread:
        if isinstance(channel, (TextChannel, Thread)):
            return channel
        error_message = "テキストチャンネルまたはスレッド内で実行してください。"
        raise ArgumentError(error_message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Monitor(bot))
