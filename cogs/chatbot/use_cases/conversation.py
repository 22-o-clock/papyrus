import asyncio
import datetime
import hashlib
from logging import getLogger

import discord
from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.constants import DEFAULT_CONVERSATION_RESET_MINUTES, EMBED_IMAGE_MAX_COUNT
from cogs.chatbot.models import ChannelProcessingState, CustomProfile
from cogs.chatbot.repositories.custom_profile import CustomProfileRepository
from cogs.chatbot.repositories.environment import DatabaseEnvironmentRepository
from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocumentRepository
from cogs.chatbot.repositories.reply_conversation import ReplyConversationRepository
from cogs.chatbot.repositories.short_term_message import (
    ChatbotShortTermMessageRepository,
    StoredAttachmentInput,
    StoredMessageInput,
    StoredReactionSnapshotInput,
)
from cogs.chatbot.responses_api import (
    AttachmentInMemory,
    MessageInMemory,
    ReactionInMemory,
    ShortTermMemory,
)
from cogs.chatbot.services.conversation_tools import ConversationTools
from cogs.chatbot.services.history_sync import get_history_sync_after
from cogs.chatbot.services.reaction_context import (
    collect_message_reactions,
    preserve_known_reactors,
)
from cogs.chatbot.services.response_policy import (
    get_available_referenced_author_id,
    should_reset_conversation,
)
from core.runtime_environment import RuntimeEnvironment, get_runtime_environment

from .attachment import AttachmentUseCases
from .custom_profile import (
    CustomProfileNotFoundError,
    CustomProfileUseCases,
    InvalidCustomProfileDirectiveError,
)
from .long_term_memory import LongTermMemoryUseCases
from .reply_conversation import ReplyConversationUseCases

logger = getLogger(__name__)

GENERIC_GENERATION_FAILURE_MESSAGE = "回答の生成に失敗しました。しばらくしてからもう一度お試しください。"
INSUFFICIENT_QUOTA_MESSAGE = (
    "OpenAI APIの利用クォータが不足しているため、回答を生成できません。管理者が請求設定または利用上限を確認してください。"
)


def get_generation_failure_message(error: Exception) -> str:
    """利用者が対処可能なOpenAIエラーだけを具体的な案内へ変換します。"""
    if isinstance(error, RateLimitError) and error.code == "insufficient_quota":
        return INSUFFICIENT_QUOTA_MESSAGE
    return GENERIC_GENERATION_FAILURE_MESSAGE


def get_mentioned_bot_role_ids(message: Message, bot_user: discord.ClientUser) -> set[int]:
    """Botに付与された同名ロールのうち、投稿でメンションされたIDを返します。"""
    guild = message.guild
    bot_member = guild.me if guild is not None else None
    if bot_member is None:
        return set()

    bot_role_ids = {role.id for role in bot_member.roles if role.name == bot_user.name}
    return {role.id for role in message.role_mentions if role.id in bot_role_ids}


class ConversationUseCases:
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.runtime_environment: RuntimeEnvironment = get_runtime_environment()
        self.short_term_memories: dict[int, ShortTermMemory] = {}
        self.environment_repository = DatabaseEnvironmentRepository(session_factory)
        self.reply_conversations = ReplyConversationUseCases(
            AsyncOpenAI(),
            ReplyConversationRepository(session_factory, "uninitialized"),
        )
        self.short_term_message_repository = ChatbotShortTermMessageRepository(session_factory)
        self.long_term_memory_repository = ChatbotMemoryDocumentRepository(session_factory)
        self.member_alias_repository = ChatbotMemberAliasRepository(session_factory)
        self.custom_profile_use_cases = CustomProfileUseCases(
            CustomProfileRepository(session_factory),
            self.runtime_environment,
        )
        self._history_sync_complete = asyncio.Event()
        self._history_sync_lock = asyncio.Lock()

        self._initialization_lock = asyncio.Lock()
        self._channel_states: dict[int, ChannelProcessingState] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._sent_custom_profiles: dict[int, str | None] = {}
        self._long_term_memory_excluded_message_ids: set[int] = set()
        self.long_term_memory_use_cases = LongTermMemoryUseCases(
            self.bot,
            self.environment_repository,
            session_factory,
            self._background_tasks,
        )
        self.attachment_use_cases = AttachmentUseCases(
            self.short_term_message_repository,
            self.short_term_memories,
            self._background_tasks,
        )

    async def _ensure_channel_state(self, channel_id: int) -> ChannelProcessingState:
        """履歴保存に使うチャンネルの短期記憶と排他制御を初期化します。"""
        if channel_id not in self._channel_states:
            async with self._initialization_lock:
                if channel_id not in self._channel_states:
                    self.short_term_memories[channel_id] = ShortTermMemory()
                    stored_messages = await self.short_term_message_repository.get_for_channel(channel_id)
                    stored_attachments = await self.short_term_message_repository.get_attachments(
                        [stored.message_id for stored in stored_messages]
                    )
                    stored_reaction_snapshots = await self.short_term_message_repository.get_reaction_snapshots(
                        [stored.message_id for stored in stored_messages]
                    )
                    attachments_by_message_id: dict[int, list[AttachmentInMemory]] = {}
                    for attachment in stored_attachments:
                        attachments_by_message_id.setdefault(attachment.message_id, []).append(
                            AttachmentInMemory(
                                attachment_id=attachment.id,
                                filename=attachment.filename,
                                kind=attachment.kind,
                                analysis_status=attachment.analysis_status,
                                summary=self.attachment_use_cases.truncate_context(attachment.summary),
                                important_text=self.attachment_use_cases.truncate_context(attachment.important_text),
                            )
                        )
                    reactions_by_message_id = {
                        snapshot.message_id: [
                            ReactionInMemory.from_dict(reaction)
                            for reaction in snapshot.reactions
                            if isinstance(reaction, dict)
                        ]
                        for snapshot in stored_reaction_snapshots
                    }
                    self.short_term_memories[channel_id].restore(
                        [
                            MessageInMemory(
                                message_id=stored.message_id,
                                author_id=stored.author_id,
                                author_name=stored.author_name,
                                content=stored.content,
                                reply_to_message_id=stored.reply_to_message_id,
                                mentioned_user_ids=stored.mentioned_user_ids,
                                timestamp=stored.created_at,
                                is_bot=stored.is_bot,
                                is_forwarded=stored.is_forwarded,
                                embeds=stored.embeds,
                                attachments=attachments_by_message_id.get(stored.message_id, []),
                                reactions=reactions_by_message_id.get(stored.message_id, []),
                            )
                            for stored in stored_messages
                        ]
                    )
                    last_human_message = next(
                        (stored for stored in reversed(stored_messages) if not stored.is_bot),
                        None,
                    )
                    self._channel_states[channel_id] = ChannelProcessingState(
                        last_human_message_timestamp=(last_human_message.created_at if last_human_message is not None else None)
                    )
        return self._channel_states[channel_id]

    async def on_ready(self) -> None:
        """会話の保存先を設定し、履歴と長期記憶の更新を初期化します。"""
        if self.bot.user:
            self.reply_conversations.repository.namespace = str(self.bot.user.id)
        self._history_sync_complete.clear()
        if self.runtime_environment.is_debug:
            logger.info("Skipped chatbot Discord history sync in debug environment")
            try:
                await self._initialize_long_term_memory_if_enabled()
            finally:
                self._history_sync_complete.set()
            return
        try:
            async with self._history_sync_lock:
                await self._synchronize_recent_discord_history()
        finally:
            try:
                await self._initialize_long_term_memory_if_enabled()
            finally:
                # 一部チャンネルの失敗で、通常の応答まで永続的に停止させない。
                self._history_sync_complete.set()

    async def _initialize_long_term_memory_if_enabled(self) -> None:
        """共有記憶を書き換えるバックグラウンド処理を本番だけで開始します。"""
        if self.runtime_environment.is_debug:
            logger.info("Disabled chatbot long-term memory workers in debug environment")
            return
        await self.long_term_memory_use_cases.initialize()

    async def _synchronize_recent_discord_history(self) -> None:
        """停止中の投稿をDiscordから取得し、応答せず通常の保存経路へ流します。"""
        now = datetime.datetime.now(datetime.UTC)
        synchronized_message_count = 0
        for guild in self.bot.guilds:
            bot_member = guild.me
            if bot_member is None:
                logger.warning("Skipped chatbot history sync because bot member is unavailable (guild_id=%s)", guild.id)
                continue
            channels: list[discord.TextChannel | discord.Thread] = [*guild.text_channels, *guild.threads]
            for channel in channels:
                if not self.runtime_environment.should_process_chatbot_channel(channel.id):
                    continue
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.read_message_history:
                    continue
                try:
                    latest_stored_at = await self.short_term_message_repository.get_latest_created_at(channel.id)
                    after = get_history_sync_after(latest_stored_at, now)
                    state = await self._ensure_channel_state(channel.id)
                    await self._refresh_retained_message_reactions(channel)
                    channel_message_count = 0
                    async for message in channel.history(after=after, oldest_first=True, limit=None):
                        await self._append_message_to_short_term_memory(message, state)
                        channel_message_count += 1
                    synchronized_message_count += channel_message_count
                    if channel_message_count:
                        logger.info(
                            "Synchronized chatbot Discord history (channel_id=%s, message_count=%s, after=%s)",
                            channel.id,
                            channel_message_count,
                            after.isoformat(),
                        )
                except Exception:
                    logger.exception("Failed to synchronize chatbot Discord history (channel_id=%s)", channel.id)
        logger.info("Completed chatbot Discord history sync (message_count=%s)", synchronized_message_count)

    async def _refresh_retained_message_reactions(
        self,
        channel: discord.TextChannel | discord.Thread,
    ) -> None:
        """再起動中の変更を補うため、実際に残る短期文脈だけDiscordと照合します。"""
        short_term_memory = self.short_term_memories[channel.id]
        if not short_term_memory.memory:
            return
        retained_messages = {message.message_id: message for message in short_term_memory.memory}
        oldest_timestamp = min(message.timestamp for message in retained_messages.values())
        snapshots: list[StoredReactionSnapshotInput] = []
        async for message in channel.history(
            after=oldest_timestamp - datetime.timedelta(microseconds=1),
            oldest_first=True,
            limit=None,
        ):
            stored_message = retained_messages.get(message.id)
            if stored_message is None:
                continue
            reactions = await collect_message_reactions(message)
            preserve_known_reactors(reactions, stored_message.reactions)
            short_term_memory.set_reactions(message.id, reactions)
            snapshots.append(
                StoredReactionSnapshotInput(
                    message_id=message.id,
                    reactions=[reaction.to_dict() for reaction in reactions],
                )
            )
        short_term_memory.forget()
        await self.short_term_message_repository.save_reaction_snapshots(snapshots)

    async def on_message(self, message: Message) -> None:
        if not self.runtime_environment.should_process_chatbot_channel(message.channel.id):
            return
        # 起動直後の不完全な文脈で応答せず、履歴同期後に受信イベントを処理する。
        await self._history_sync_complete.wait()
        # 明示的に呼ばれる前の投稿も長期記憶用に保存する。
        state = await self._ensure_channel_state(message.channel.id)

        async with state.lock:
            await self._append_message_to_short_term_memory(message, state)

        if self.runtime_environment.is_production:
            await self._enqueue_long_term_memory(message)

        await self._respond_to_explicit_call(message)

    async def _respond_to_explicit_call(self, message: Message) -> None:
        """Botへのメンションまたは返信に回答し、使用したプロファイルを記録します。"""
        # 3. 回答を行うかの判定
        # 3.1 ボットのメッセージについては返信しない
        if message.author.bot:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        directly_mentioned_bot = any(user.id == bot_user.id for user in message.mentions)
        mentioned_bot_role_ids = get_mentioned_bot_role_ids(message, bot_user)
        mentioned_bot = directly_mentioned_bot or bool(mentioned_bot_role_ids)
        replied_to_bot = await self._is_reply_to_bot(message)
        is_explicit_call = mentioned_bot or replied_to_bot
        custom_profile: CustomProfile | None = None
        if mentioned_bot:
            try:
                custom_profile = await self.custom_profile_use_cases.resolve(
                    message.content,
                    message_id=message.id,
                    bot_user_id=bot_user.id,
                    directly_mentioned=True,
                    bot_role_ids=mentioned_bot_role_ids,
                )
            except InvalidCustomProfileDirectiveError as exc:
                await message.reply(self._custom_profile_directive_error_message(exc))
                return
            except CustomProfileNotFoundError as exc:
                await message.reply(f"カスタムプロファイル `{exc.args[0]}` は見つかりませんでした。")
                return
            except Exception:
                logger.exception(
                    "Failed to resolve chatbot custom profile (message_id=%s, channel_id=%s)",
                    message.id,
                    message.channel.id,
                )
                await message.reply("カスタムプロファイルを一時的に利用できません。しばらくしてから再度お試しください。")
                return

        if not is_explicit_call or message.guild is None:
            return
        tools = ConversationTools(
            message.guild,
            self.short_term_message_repository,
            self.long_term_memory_repository,
            bot_user.id,
            self.member_alias_repository,
        )
        try:
            sent, profile_name = await self.reply_conversations.respond(message, tools, custom_profile)
            await self._record_sent_profiles(sent, profile_name)
        except Exception as exc:
            logger.exception("Failed to generate reply conversation (message_id=%s)", message.id)
            await message.reply(get_generation_failure_message(exc))

    @staticmethod
    def _custom_profile_directive_error_message(error: InvalidCustomProfileDirectiveError) -> str:
        """option構文の検証結果を利用者向けメッセージへ変換します。"""
        messages = {
            "missing_name": "`option` の後にプロファイル名を指定してください。",
            "invalid_name": "プロファイル名には英数字、`_`、`-`だけを使用できます。",
            "missing_content": "プロファイル指定の次の行に、回答してほしい本文を入力してください。",
        }
        return messages[error.reason.value]

    async def on_message_delete(self, message: Message) -> None:
        """削除された投稿を短期保存と現在の短期記憶から除去します。"""
        if not self.runtime_environment.should_process_chatbot_channel(message.channel.id):
            return
        await self.short_term_message_repository.delete(message.id)
        if self.runtime_environment.is_production:
            await self.long_term_memory_use_cases.delete(message.id)
        state = self._channel_states.get(message.channel.id)
        if state is None:
            return

        async with state.lock:
            self.short_term_memories[message.channel.id].remove(message.id)

    async def on_message_edit(self, before: Message, after: Message) -> None:
        """編集された投稿を短期保存と現在の短期記憶へ反映します。"""
        if not self.runtime_environment.should_process_chatbot_channel(after.channel.id):
            return
        state = await self._ensure_channel_state(after.channel.id)
        async with state.lock:
            short_term_memory = self.short_term_memories[after.channel.id]
            short_term_memory.remove(before.id)
            await short_term_memory.append(after)
            stored_message = short_term_memory.get_message(after.id)
            if stored_message is None:
                return
            await self.short_term_message_repository.save(
                StoredMessageInput(
                    message_id=stored_message.message_id,
                    channel_id=after.channel.id,
                    author_id=stored_message.author_id,
                    author_name=stored_message.author_name,
                    content=stored_message.content,
                    reply_to_message_id=stored_message.reply_to_message_id,
                    mentioned_user_ids=stored_message.mentioned_user_ids,
                    created_at=stored_message.timestamp,
                    is_bot=after.author.bot,
                    is_self=self._is_self_message(after),
                    is_forwarded=bool(after.message_snapshots),
                    is_long_term_memory_excluded=after.id in self._long_term_memory_excluded_message_ids,
                    custom_profile_name=self._sent_custom_profiles.get(after.id),
                    embeds=self._serialize_embeds(after),
                )
            )
            await self._synchronize_message_reactions(after)
            await self.short_term_message_repository.delete_attachments(after.id)
            await self._save_message_media(after)

            if self.runtime_environment.is_production:
                await self._enqueue_long_term_memory(after)

    async def _is_reply_to_bot(self, message: Message) -> bool:
        """受信したメッセージが、このボットの発言へのDiscord返信か判定します。

        Args:
            message: on_messageで受信した判定対象のメッセージ。

        Returns:
            返信元の投稿者がこのボットの場合はTrue。
            通常投稿または他ユーザーへの返信の場合はFalse。

        """
        reference = message.reference
        bot_user = self.bot.user
        if message.type != discord.MessageType.reply or reference is None or reference.message_id is None or bot_user is None:
            return False

        referenced_author_id = get_available_referenced_author_id(reference)
        if referenced_author_id is not None:
            return referenced_author_id == bot_user.id

        short_term_memory = self.short_term_memories[message.channel.id]
        referenced_author_id = short_term_memory.get_author_id(reference.message_id)
        if referenced_author_id is not None:
            return referenced_author_id == bot_user.id

        try:
            referenced_message = await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            logger.warning(
                "Failed to fetch replied message (message_id=%s, channel_id=%s)",
                reference.message_id,
                message.channel.id,
            )
            return False

        return referenced_message.author.id == bot_user.id

    async def _append_message_to_short_term_memory(self, message: Message, state: ChannelProcessingState) -> None:
        """人間投稿の長時間の空白を検出し、必要に応じて短期文脈をリセットしてから保存します。"""
        short_term_memory = self.short_term_memories[message.channel.id]
        if short_term_memory.contains_message(message.id):
            return
        if not message.author.bot and should_reset_conversation(
            state.last_human_message_timestamp,
            message.created_at,
            DEFAULT_CONVERSATION_RESET_MINUTES,
        ):
            short_term_memory.reset_for_new_conversation()

        await short_term_memory.append(message)
        stored_message = short_term_memory.get_message(message.id)
        if stored_message is not None:
            await self.short_term_message_repository.save(
                StoredMessageInput(
                    message_id=stored_message.message_id,
                    channel_id=message.channel.id,
                    author_id=stored_message.author_id,
                    author_name=stored_message.author_name,
                    content=stored_message.content,
                    reply_to_message_id=stored_message.reply_to_message_id,
                    mentioned_user_ids=stored_message.mentioned_user_ids,
                    created_at=stored_message.timestamp,
                    is_bot=message.author.bot,
                    is_self=self._is_self_message(message),
                    is_forwarded=bool(message.message_snapshots),
                    is_long_term_memory_excluded=message.id in self._long_term_memory_excluded_message_ids,
                    custom_profile_name=self._sent_custom_profiles.get(message.id),
                    embeds=self._serialize_embeds(message),
                )
            )
            await self._synchronize_message_reactions(message)
            await self._save_message_media(message)
            self._long_term_memory_excluded_message_ids.discard(message.id)
        if not message.author.bot:
            state.last_human_message_timestamp = message.created_at

    def _is_self_message(self, message: Message) -> bool:
        bot_user = self.bot.user
        return bot_user is not None and message.author.id == bot_user.id

    async def exclude_from_long_term_memory(self, message: Message) -> None:
        """他機能が生成した投稿を長期記憶から除外し、イベント順の競合も吸収します。"""
        self._long_term_memory_excluded_message_ids.add(message.id)
        await self.short_term_message_repository.exclude_from_long_term_memory(message.id)
        asyncio.get_running_loop().call_later(
            60,
            self._long_term_memory_excluded_message_ids.discard,
            message.id,
        )

    async def _enqueue_long_term_memory(self, message: Message) -> None:
        """人間またはPapyrus自身の投稿だけを文書更新の起点にします。"""
        is_human = not message.author.bot
        if message.message_snapshots or (not is_human and not self._is_self_message(message)):
            return
        await self.long_term_memory_use_cases.enqueue(
            message.id,
            message.channel.id,
            is_human=is_human,
            created_at=message.created_at,
        )

    @staticmethod
    def _serialize_embeds(message: Message) -> list[dict[str, object]]:
        """Discordが展開したEmbedを会話理解に必要な項目だけ保存します。"""
        return [
            {
                "type": embed.type,
                "url": embed.url,
                "provider": embed.provider.name if embed.provider is not None else None,
                "author": embed.author.name if embed.author is not None else None,
                "title": embed.title,
                "description": embed.description,
                "fields": [{"name": field.name, "value": field.value} for field in embed.fields],
                "footer": embed.footer.text if embed.footer is not None else None,
                "timestamp": embed.timestamp.isoformat() if embed.timestamp is not None else None,
                "image_url": embed.image.url if embed.image is not None else None,
                "thumbnail_url": embed.thumbnail.url if embed.thumbnail is not None else None,
            }
            for embed in message.embeds
        ]

    async def _save_message_media(self, message: Message) -> None:
        """通常添付とEmbed画像を保存し、重複URLを除いて解析します。"""
        media: list[tuple[int, str, str, str | None, str]] = []
        for attachment in message.attachments:
            kind = self.attachment_use_cases.get_kind(attachment.content_type)
            if kind is not None:
                media.append((attachment.id, attachment.url, attachment.filename, attachment.content_type, kind))

        seen_urls = {url for _, url, _, _, _ in media}
        embed_image_urls: list[str] = []
        for embed in message.embeds:
            for image in (embed.image, embed.thumbnail):
                url = image.url if image is not None else None
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    embed_image_urls.append(url)
                    if len(embed_image_urls) == EMBED_IMAGE_MAX_COUNT:
                        break
            if len(embed_image_urls) == EMBED_IMAGE_MAX_COUNT:
                break
        for position, url in enumerate(embed_image_urls):
            digest = hashlib.blake2b(f"{message.id}:{url}".encode(), digest_size=8).digest()
            attachment_id = -(int.from_bytes(digest, "big", signed=False) & ((1 << 63) - 1))
            media.append((attachment_id, url, f"embed-image-{position + 1}", None, "image"))

        for attachment_id, url, filename, content_type, kind in media:
            await self.short_term_message_repository.save_attachment(
                StoredAttachmentInput(
                    id=attachment_id,
                    message_id=message.id,
                    url=url,
                    filename=filename,
                    content_type=content_type,
                    kind=kind,
                )
            )
            self.attachment_use_cases.schedule(message.id, attachment_id, filename, url, kind)

    async def _record_sent_profiles(
        self,
        messages: list[Message],
        custom_profile_name: str | None,
    ) -> None:
        """生成済みPapyrus投稿が自己記憶の根拠になるか判別できるよう記録します。"""
        message_ids = [message.id for message in messages]
        for message_id in message_ids:
            self._sent_custom_profiles[message_id] = custom_profile_name
        await self.short_term_message_repository.set_custom_profile(message_ids, custom_profile_name)

    async def on_raw_reaction_change(self, message_id: int, channel_id: int) -> None:
        """リアクションイベントを応答開始条件にせず、短期文脈だけ更新します。"""
        if not self.runtime_environment.should_process_chatbot_channel(channel_id):
            return
        await self._history_sync_complete.wait()
        if not await self.short_term_message_repository.contains(message_id):
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            logger.warning(
                "Failed to fetch reacted chatbot message (message_id=%s, channel_id=%s)",
                message_id,
                channel_id,
                exc_info=True,
            )
            return
        state = self._channel_states.get(channel_id)
        if state is None:
            reactions = await collect_message_reactions(message)
            snapshots = await self.short_term_message_repository.get_reaction_snapshots([message_id])
            previous_reactions = (
                [ReactionInMemory.from_dict(reaction) for reaction in snapshots[0].reactions if isinstance(reaction, dict)]
                if snapshots
                else []
            )
            preserve_known_reactors(reactions, previous_reactions)
            await self._save_message_reactions(message_id, reactions)
            return
        async with state.lock:
            await self._synchronize_message_reactions(message)

    async def _synchronize_message_reactions(self, message: Message) -> None:
        """Discord上の最新リアクションを短期記憶とDBへ反映します。"""
        reactions = await collect_message_reactions(message)
        short_term_memory = self.short_term_memories[message.channel.id]
        stored_message = short_term_memory.get_message(message.id)
        previous_reactions = stored_message.reactions if stored_message is not None else []
        preserve_known_reactors(reactions, previous_reactions)
        short_term_memory.set_reactions(message.id, reactions)
        short_term_memory.forget()
        await self._save_message_reactions(message.id, reactions)

    async def _save_message_reactions(self, message_id: int, reactions: list[ReactionInMemory]) -> None:
        """リアクションの構造化スナップショットを永続化します。"""
        await self.short_term_message_repository.save_reactions(
            StoredReactionSnapshotInput(
                message_id=message_id,
                reactions=[reaction.to_dict() for reaction in reactions],
            )
        )
