import asyncio
import datetime
import hashlib
from dataclasses import dataclass
from logging import getLogger

import discord
from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.channel_roles import ChannelRole, ChannelRoleManager
from cogs.chatbot.constants import DEFAULT_CONVERSATION_RESET_MINUTES, EMBED_IMAGE_MAX_COUNT
from cogs.chatbot.models import ChannelProcessingState, CustomProfile, ResponseMode, ResponseRequestOptions
from cogs.chatbot.repositories.custom_profile import CustomProfileRepository
from cogs.chatbot.repositories.environment import DatabaseEnvironmentRepository
from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository, find_member_aliases
from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocumentRepository
from cogs.chatbot.repositories.short_term_message import (
    ChatbotShortTermMessageRepository,
    StoredAttachmentInput,
    StoredMessageInput,
    StoredReactionSnapshotInput,
)
from cogs.chatbot.responses_api import (
    AttachmentInMemory,
    LLMMessage,
    MessageInMemory,
    ReactionInMemory,
    ResponseAction,
    ResponseGenerationOptions,
    ResponsePipeline,
)
from cogs.chatbot.services.history_sync import get_history_sync_after
from cogs.chatbot.services.message_delivery import reply_with_split_response, send_split_response
from cogs.chatbot.services.reaction_context import (
    collect_message_reactions,
    preserve_known_reactors,
    resolve_reaction_emoji,
)
from cogs.chatbot.services.response_policy import (
    activate_response,
    claim_response_slot,
    get_available_referenced_author_id,
    get_response_debounce_seconds,
    is_generation_current,
    should_reset_conversation,
    should_respond,
)
from core.runtime_environment import RuntimeEnvironment, get_runtime_environment

from .attachment import AttachmentUseCases
from .custom_profile import (
    CustomProfileNotFoundError,
    CustomProfileUseCases,
    InvalidCustomProfileDirectiveError,
)
from .long_term_memory import LongTermMemoryUseCases
from .memory_search import MemorySearchUseCases
from .settings import SettingsUseCases

logger = getLogger(__name__)

GENERIC_GENERATION_FAILURE_MESSAGE = "回答の生成に失敗しました。しばらくしてからもう一度お試しください。"
INSUFFICIENT_QUOTA_MESSAGE = (
    "OpenAI APIの利用クォータが不足しているため、回答を生成できません。管理者が請求設定または利用上限を確認してください。"
)


@dataclass(frozen=True, slots=True)
class _ResponseSelectionRequest:
    """応答種別判定時点で固定する入力。"""

    generation_revision: int
    is_explicit_call: bool
    resolved_member_aliases: dict[str, int]


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


def _collect_available_custom_emojis(message: Message, response_mode: ResponseMode) -> tuple[str, ...]:
    """リアクション生成時にLLMへ提示する、利用可能なサーバーのカスタム絵文字一覧を返します。"""
    if response_mode is not ResponseMode.REACTION or message.guild is None:
        return ()
    return tuple(
        f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>" for emoji in message.guild.emojis if emoji.available
    )


class ConversationUseCases:
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.runtime_environment: RuntimeEnvironment = get_runtime_environment()
        self.response_pipelines: dict[int, ResponsePipeline] = {}
        self.environment_repository = DatabaseEnvironmentRepository(session_factory)
        self.channel_role_manager = ChannelRoleManager(self.environment_repository)
        self.settings_use_cases = SettingsUseCases(
            self.channel_role_manager,
            self.runtime_environment,
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
            self.response_pipelines,
            self._background_tasks,
        )
        self.memory_search_use_cases = MemorySearchUseCases(
            self.bot,
            self.response_pipelines,
            self.long_term_memory_repository,
            self.member_alias_repository,
            self.short_term_message_repository,
        )

    async def initialize_response_pipeline_for_channel(self, channel_id: int) -> None:
        if self.bot.user:
            self.response_pipelines[channel_id] = ResponsePipeline(AsyncOpenAI(), self.bot.user.display_name)
        else:
            logger.warning(
                "Bot user is not available during initialization, response pipeline may not be initialized correctly"
            )
            self.response_pipelines[channel_id] = ResponsePipeline(AsyncOpenAI(), "Bot")

    async def _ensure_channel_state(self, channel_id: int) -> ChannelProcessingState:
        """チャンネルの応答パイプラインと処理状態を一度だけ初期化します。"""
        if channel_id not in self._channel_states:
            async with self._initialization_lock:
                if channel_id not in self._channel_states:
                    await self.initialize_response_pipeline_for_channel(channel_id)
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
                    self.response_pipelines[channel_id].short_term_memory.restore(
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
        """保存済み設定を読み込み、会話履歴を初期化します。"""
        await self.settings_use_cases.initialize()
        self._history_sync_complete.clear()
        if self.runtime_environment.is_debug:
            logger.info("Skipped chatbot Discord history sync in debug environment")
            self._history_sync_complete.set()
            await self._initialize_long_term_memory_if_enabled()
            return
        try:
            async with self._history_sync_lock:
                await self._synchronize_recent_discord_history()
        finally:
            # 一部チャンネルの失敗で、通常の応答まで永続的に停止させない。
            self._history_sync_complete.set()
        await self._initialize_long_term_memory_if_enabled()

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
                        if self.runtime_environment.is_production:
                            await self._enqueue_long_term_memory(message)
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
        short_term_memory = self.response_pipelines[channel.id].short_term_memory
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
        # 1. 明示的に呼ばれる前の会話も保持するため、チャンネルごとにパイプラインを遅延初期化
        state = await self._ensure_channel_state(message.channel.id)

        # 2. 同じチャンネルで回答を生成中の場合、生成中の文脈を変えないようメッセージを保留
        async with state.lock:
            if state.generating:
                state.pending_messages.append(message)
            else:
                await self._append_message_to_short_term_memory(message, state)

        if self.runtime_environment.is_production:
            await self._enqueue_long_term_memory(message)

        # 3. 回答を行うかの判定
        # 3.1 ボットのメッセージについては返信しない
        if message.author.bot:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        role = await self.channel_role_manager.get_role(message.channel.id)
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

        response_required = should_respond(
            role,
            mentioned_bot=mentioned_bot,
            replied_to_bot=replied_to_bot,
        )
        await self._update_response_schedule(
            message if response_required else None,
            state,
            role,
            options=ResponseRequestOptions(
                is_explicit_call=is_explicit_call,
                custom_profile=custom_profile,
            ),
        )

    @staticmethod
    def _custom_profile_directive_error_message(error: InvalidCustomProfileDirectiveError) -> str:
        """option構文の検証結果を利用者向けメッセージへ変換します。"""
        messages = {
            "missing_name": "`option` の後にプロファイル名を指定してください。",
            "invalid_name": "プロファイル名には英数字、`_`、`-`だけを使用できます。",
            "missing_content": "プロファイル指定の次の行に、回答してほしい本文を入力してください。",
        }
        return messages[error.reason.value]

    async def _update_response_schedule(
        self,
        response_message: Message | None,
        state: ChannelProcessingState,
        role: ChannelRole,
        options: ResponseRequestOptions,
    ) -> None:
        """返信対象を更新し、最後の人間投稿から一定時間後に生成を開始します。"""
        async with state.lock:
            if state.generating:
                if response_message is not None:
                    claim_response_slot(
                        state,
                        response_message,
                        is_explicit_call=options.is_explicit_call,
                        custom_profile=options.custom_profile,
                    )
                return

            if response_message is not None and (options.is_explicit_call or not state.debounced_response_is_explicit_call):
                state.debounced_response_message = response_message
                state.debounced_response_is_explicit_call = options.is_explicit_call
                state.debounced_custom_profile = options.custom_profile
            if state.debounced_response_message is None:
                return

            if state.debounce_task is not None:
                state.debounce_task.cancel()

            delay_seconds = get_response_debounce_seconds(role)
            task = asyncio.create_task(self._start_response_after_delay(state, delay_seconds))
            state.debounce_task = task

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _start_response_after_delay(
        self,
        state: ChannelProcessingState,
        delay_seconds: float,
    ) -> None:
        """デバウンス時間の経過後、最新の返信対象に対する生成を開始します。"""
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return

        async with state.lock:
            if state.debounce_task is not asyncio.current_task():
                return

            message = state.debounced_response_message
            is_explicit_call = state.debounced_response_is_explicit_call
            custom_profile = state.debounced_custom_profile
            state.debounce_task = None
            state.debounced_response_message = None
            state.debounced_response_is_explicit_call = False
            state.debounced_custom_profile = None
            if message is None or not claim_response_slot(
                state,
                message,
                is_explicit_call=is_explicit_call,
                custom_profile=custom_profile,
            ):
                return

        await self._process_response_queue(
            message,
            state,
            is_explicit_call=is_explicit_call,
            custom_profile=custom_profile,
        )

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
            self.response_pipelines[message.channel.id].short_term_memory.remove(message.id)

    async def on_message_edit(self, before: Message, after: Message) -> None:
        """編集された投稿を短期保存と現在の短期記憶へ反映します。"""
        if not self.runtime_environment.should_process_chatbot_channel(after.channel.id):
            return
        state = await self._ensure_channel_state(after.channel.id)
        async with state.lock:
            short_term_memory = self.response_pipelines[after.channel.id].short_term_memory
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

        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
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

    async def _process_response_queue(
        self,
        message: Message,
        state: ChannelProcessingState,
        *,
        is_explicit_call: bool,
        custom_profile: CustomProfile | None,
    ) -> None:
        """同一チャンネルの返信要求を順番に生成し、保留メッセージを文脈へ反映します。"""
        current_message = message
        current_is_explicit_call = is_explicit_call
        current_custom_profile = custom_profile
        completed_normally = False
        try:
            while True:
                await self._generate_and_send_response(
                    current_message,
                    state,
                    is_explicit_call=current_is_explicit_call,
                    custom_profile=current_custom_profile,
                )

                async with state.lock:
                    await self._flush_pending_messages(state)
                    next_message = state.queued_response_message
                    next_is_explicit_call = state.queued_response_is_explicit_call
                    next_custom_profile = state.queued_custom_profile
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    state.queued_custom_profile = None
                    if next_message is None:
                        state.generating = False
                        self._clear_active_response(state)
                        completed_normally = True
                        return
                    current_message = next_message
                    current_is_explicit_call = next_is_explicit_call
                    current_custom_profile = next_custom_profile
                    activate_response(
                        state,
                        current_message,
                        is_explicit_call=current_is_explicit_call,
                        custom_profile=current_custom_profile,
                    )
        finally:
            if not completed_normally:
                async with state.lock:
                    await self._flush_pending_messages(state)
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    state.queued_custom_profile = None
                    state.generating = False
                    self._clear_active_response(state)

    @staticmethod
    def _clear_active_response(state: ChannelProcessingState) -> None:
        """処理済みの応答要求を状態から除去します。"""
        state.active_response_message = None
        state.active_response_is_explicit_call = False
        state.active_custom_profile = None

    async def _flush_pending_messages(self, state: ChannelProcessingState) -> None:
        """生成中に保留したメッセージを時系列順で短期記憶へ移します。"""
        for pending_message in sorted(state.pending_messages, key=lambda pending: pending.id):
            await self._append_message_to_short_term_memory(pending_message, state)
        state.pending_messages.clear()

    async def _append_message_to_short_term_memory(self, message: Message, state: ChannelProcessingState) -> None:
        """人間投稿の長時間の空白を検出し、必要に応じて短期文脈をリセットしてから保存します。"""
        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
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
        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
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

    async def _generate_and_send_response(
        self,
        message: Message,
        state: ChannelProcessingState,
        *,
        is_explicit_call: bool,
        custom_profile: CustomProfile | None,
    ) -> None:
        """要否判定後に回答を生成し、文脈が更新されていなければ送信します。"""
        generation_revision = state.generation_revision
        role = await self.channel_role_manager.get_role(message.channel.id)
        resolved_member_aliases = await self._resolve_member_aliases(message.channel.id)
        required_reply_to_message_id = message.id if is_explicit_call else None
        response_mode = await self._select_response_mode(
            message,
            state,
            _ResponseSelectionRequest(
                generation_revision=generation_revision,
                is_explicit_call=is_explicit_call,
                resolved_member_aliases=resolved_member_aliases,
            ),
        )
        if response_mode is None:
            return

        async def generate_response() -> LLMMessage:
            response_memory = await self.memory_search_use_cases.build_response_memory(
                message.channel.id,
                resolved_member_aliases,
            )
            return await self.response_pipelines[message.channel.id].generate_response(
                role,
                ResponseGenerationOptions(
                    response_mode=response_mode,
                    required_reply_to_message_id=required_reply_to_message_id,
                    long_term_memory_context=response_memory.long_term_memory,
                    pending_other_channel_index=response_memory.pending_index,
                    pending_other_channel_context=response_memory.pending_context,
                    custom_profile=custom_profile,
                    resolved_member_aliases=resolved_member_aliases,
                    available_custom_emojis=_collect_available_custom_emojis(message, response_mode),
                ),
            )

        try:
            if response_mode is ResponseMode.TEXT:
                async with message.channel.typing():
                    generated_response = await generate_response()
            else:
                generated_response = await generate_response()
        except Exception as error:
            logger.exception(
                "Failed to generate chatbot response (channel_id=%s, trigger_message_id=%s, explicit=%s)",
                message.channel.id,
                message.id,
                is_explicit_call,
            )
            if is_explicit_call and is_generation_current(state, generation_revision):
                try:
                    await message.reply(get_generation_failure_message(error))
                except discord.HTTPException:
                    logger.exception(
                        "Failed to notify explicit chatbot response failure (trigger_message_id=%s)",
                        message.id,
                    )
            return

        async with state.lock:
            if not is_generation_current(state, generation_revision):
                return
            await self.execute_response_action(
                message,
                generated_response,
                custom_profile_name=custom_profile.name if custom_profile is not None else None,
            )
            if is_explicit_call:
                state.active_response_is_explicit_call = False

    async def _select_response_mode(
        self,
        message: Message,
        state: ChannelProcessingState,
        request: _ResponseSelectionRequest,
    ) -> ResponseMode | None:
        """明示呼びかけを優先し、それ以外は固定したクールダウン段階で判定します。"""
        if request.is_explicit_call:
            logger.info(
                "Bypassed chatbot response judgment for explicit call (channel_id=%s, trigger_message_id=%s)",
                message.channel.id,
                message.id,
            )
            return ResponseMode.TEXT

        judgment = await self.response_pipelines[message.channel.id].judge_response(
            resolved_member_aliases=request.resolved_member_aliases,
        )
        logger.info(
            "Completed chatbot response judgment (channel_id=%s, trigger_message_id=%s, response_mode=%s)",
            message.channel.id,
            message.id,
            judgment.response_mode.value,
        )
        async with state.lock:
            if is_generation_current(state, request.generation_revision):
                return judgment.response_mode if judgment.response_mode is not ResponseMode.NONE else None
        logger.info(
            "Discarded stale chatbot response judgment (channel_id=%s, trigger_message_id=%s)",
            message.channel.id,
            message.id,
        )
        return None

    async def _resolve_member_aliases(self, channel_id: int) -> dict[str, int]:
        """短期会話に実際に含まれる確定済み別名だけを解決します。"""
        short_term_memory = self.response_pipelines[channel_id].short_term_memory
        active_aliases = await self.member_alias_repository.get_active_aliases()
        return find_member_aliases(short_term_memory.to_json(), active_aliases)

    async def execute_response_action(
        self,
        message: Message,
        response: LLMMessage,
        *,
        custom_profile_name: str | None = None,
    ) -> None:
        """構造化された応答行動をDiscord上で実行します。"""
        if response.action is ResponseAction.SILENCE:
            return

        if response.action is ResponseAction.MESSAGE:
            sent_messages = await send_split_response(message.channel, response.content)
            await self._record_sent_profiles(sent_messages, custom_profile_name)
            return

        reply_to_message_id = response.reply_to_message_id
        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
        if (
            reply_to_message_id is None
            or not short_term_memory.can_target_message(reply_to_message_id)
            or not isinstance(message.channel, discord.TextChannel | discord.Thread)
        ):
            logger.warning(
                "Generated response refers to unavailable message (action=%s, message_id=%s, channel_id=%s)",
                response.action.value,
                reply_to_message_id,
                message.channel.id,
            )
            return

        target_message = message.channel.get_partial_message(reply_to_message_id)
        if response.action is ResponseAction.REACTION:
            reaction_emoji = response.reaction_emoji
            if reaction_emoji is None:
                logger.warning("Generated reaction has no emoji (message_id=%s)", reply_to_message_id)
                return
            resolved_emoji = resolve_reaction_emoji(message.guild, reaction_emoji)
            await target_message.add_reaction(resolved_emoji)
            return

        sent_messages = await reply_with_split_response(target_message, response.content)
        await self._record_sent_profiles(sent_messages, custom_profile_name)
