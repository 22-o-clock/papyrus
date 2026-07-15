import asyncio
import datetime
from logging import getLogger

import discord
from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.channel_roles import ChannelRole, ChannelRoleManager
from cogs.chatbot.models import ChannelProcessingState, CustomProfile, ResponseRequestOptions
from cogs.chatbot.repositories.custom_profile import CustomProfileRepository
from cogs.chatbot.repositories.environment import DatabaseEnvironmentRepository
from cogs.chatbot.repositories.long_term_memory import ChatbotLongTermMemoryRepository
from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
from cogs.chatbot.repositories.shadow_candidate import ChatbotShadowCandidateRepository, ShadowCandidateInput
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
    ResponsePipeline,
    ShadowReason,
)
from cogs.chatbot.services.history_sync import get_history_sync_after
from cogs.chatbot.services.message_delivery import reply_with_split_response, send_split_response
from cogs.chatbot.services.reaction_context import collect_message_reactions, preserve_known_reactors
from cogs.chatbot.services.response_policy import (
    can_execute_spontaneous_action,
    can_start_spontaneous_generation,
    claim_response_slot,
    get_available_referenced_author_id,
    get_response_debounce_seconds,
    get_unanswered_question_wait_minutes,
    is_generation_current,
    is_unaddressed_question,
    should_reset_conversation,
    should_respond,
)
from cogs.chatbot.shadow_mode import ShadowModeManager

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


def get_mentioned_bot_role_ids(message: Message, bot_user: discord.ClientUser) -> set[int]:
    """Botに付与された同名ロールのうち、投稿でメンションされたIDを返します。"""
    guild = message.guild
    bot_member = guild.me if guild is not None else None
    if bot_member is None:
        return set()

    bot_role_ids = {role.id for role in bot_member.roles if role.name == bot_user.name}
    return {role.id for role in message.role_mentions if role.id in bot_role_ids}


def should_enqueue_long_term_memory(message: Message) -> bool:
    """本人へ帰属できる人間の投稿だけを長期記憶抽出へ渡します。"""
    return not message.author.bot and not message.message_snapshots


class ConversationUseCases:
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.response_pipelines: dict[int, ResponsePipeline] = {}
        self.environment_repository = DatabaseEnvironmentRepository(session_factory)
        self.channel_role_manager = ChannelRoleManager(self.environment_repository)
        self.shadow_mode_manager = ShadowModeManager(self.environment_repository)
        self.settings_use_cases = SettingsUseCases(
            self.environment_repository,
            self.channel_role_manager,
            self.shadow_mode_manager,
        )
        self.shadow_candidate_repository = ChatbotShadowCandidateRepository(session_factory)
        self.short_term_message_repository = ChatbotShortTermMessageRepository(session_factory)
        self.long_term_memory_repository = ChatbotLongTermMemoryRepository(session_factory)
        self.member_alias_repository = ChatbotMemberAliasRepository(session_factory)
        self.custom_profile_use_cases = CustomProfileUseCases(CustomProfileRepository(session_factory))
        self._history_sync_complete = asyncio.Event()
        self._history_sync_lock = asyncio.Lock()

        self._initialization_lock = asyncio.Lock()
        self._channel_states: dict[int, ChannelProcessingState] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
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
        """サーバー共通の待機時間設定を読み込みます。"""
        if not await self.settings_use_cases.initialize():
            self._history_sync_complete.set()
            return
        self._history_sync_complete.clear()
        try:
            async with self._history_sync_lock:
                await self._synchronize_recent_discord_history()
        finally:
            # 一部チャンネルの失敗で、通常の応答まで永続的に停止させない。
            self._history_sync_complete.set()
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
                        if should_enqueue_long_term_memory(message):
                            await self.long_term_memory_use_cases.enqueue(message.id, channel.id)
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

        # 3. 回答を行うかの判定
        # 3.1 ボットのメッセージについては返信しない
        if message.author.bot:
            return

        if should_enqueue_long_term_memory(message):
            await self.long_term_memory_use_cases.enqueue(message.id, message.channel.id)

        bot_user = self.bot.user
        if bot_user is None:
            return

        parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
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

        await self._cancel_unanswered_question_wait(state)
        if (
            role is ChannelRole.CHAT
            and not is_explicit_call
            and is_unaddressed_question(
                content=message.clean_content,
                is_reply=message.type == discord.MessageType.reply,
                mentioned_user_ids=[user.id for user in message.mentions],
            )
        ):
            await self._schedule_unanswered_question_wait(message, state)
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
                is_unanswered_question=False,
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
                        is_unanswered_question=options.is_unanswered_question,
                        custom_profile=options.custom_profile,
                    )
                return

            if response_message is not None:
                state.debounced_response_message = response_message
                state.debounced_response_is_explicit_call = options.is_explicit_call
                state.debounced_response_is_unanswered_question = options.is_unanswered_question
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
            is_unanswered_question = state.debounced_response_is_unanswered_question
            custom_profile = state.debounced_custom_profile
            state.debounce_task = None
            state.debounced_response_message = None
            state.debounced_response_is_explicit_call = False
            state.debounced_response_is_unanswered_question = False
            state.debounced_custom_profile = None
            if message is None or not claim_response_slot(
                state,
                message,
                is_explicit_call=is_explicit_call,
                is_unanswered_question=is_unanswered_question,
                custom_profile=custom_profile,
            ):
                return

        await self._process_response_queue(
            message,
            state,
            is_explicit_call=is_explicit_call,
            is_unanswered_question=is_unanswered_question,
            custom_profile=custom_profile,
        )

    async def _schedule_unanswered_question_wait(self, message: Message, state: ChannelProcessingState) -> None:
        """宛先のない質問への回答を、人間の反応を優先して遅延させます。"""
        async with state.lock:
            if state.debounce_task is not None and not state.debounced_response_is_explicit_call:
                state.debounce_task.cancel()
                state.debounce_task = None
                state.debounced_response_message = None
                state.debounced_response_is_unanswered_question = False
                state.debounced_custom_profile = None

            wait_minutes = get_unanswered_question_wait_minutes(
                self.settings_use_cases.unanswered_question_minimum_wait_minutes,
                self.settings_use_cases.unanswered_question_maximum_wait_minutes,
            )
            task = asyncio.create_task(self._answer_unanswered_question_after_wait(message, state, wait_minutes))
            state.unanswered_question_task = task
            state.unanswered_question_message_id = message.id

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _cancel_unanswered_question_wait(self, state: ChannelProcessingState) -> None:
        """新しい人間投稿を受けたため、待機中の宛先のない質問への回答を取り消します。"""
        async with state.lock:
            if state.unanswered_question_task is not None:
                state.unanswered_question_task.cancel()
            state.unanswered_question_task = None
            state.unanswered_question_message_id = None

    async def _answer_unanswered_question_after_wait(
        self,
        message: Message,
        state: ChannelProcessingState,
        wait_minutes: int,
    ) -> None:
        """待機後も質問が有効なら、短く答えられる場合だけ回答を生成します。"""
        try:
            await asyncio.sleep(datetime.timedelta(minutes=wait_minutes).total_seconds())
        except asyncio.CancelledError:
            return

        parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
        if role is not ChannelRole.CHAT:
            return

        async with state.lock:
            if state.unanswered_question_task is not asyncio.current_task():
                return
            state.unanswered_question_task = None
            state.unanswered_question_message_id = None
            if not claim_response_slot(
                state,
                message,
                is_explicit_call=False,
                is_unanswered_question=True,
            ):
                return

        await self._process_response_queue(
            message,
            state,
            is_explicit_call=False,
            is_unanswered_question=True,
            custom_profile=None,
        )

    async def on_message_delete(self, message: Message) -> None:
        """待機中の質問が削除された場合は、遅延した回答を取り消します。"""
        await self.short_term_message_repository.delete(message.id)
        await self.long_term_memory_use_cases.delete(message.id)
        state = self._channel_states.get(message.channel.id)
        if state is None:
            return

        async with state.lock:
            self.response_pipelines[message.channel.id].short_term_memory.remove(message.id)
            if state.unanswered_question_message_id != message.id:
                return
            if state.unanswered_question_task is not None:
                state.unanswered_question_task.cancel()
            state.unanswered_question_task = None
            state.unanswered_question_message_id = None

    async def on_message_edit(self, before: Message, after: Message) -> None:
        """編集された投稿を短期保存と現在の短期記憶へ反映します。"""
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
                )
            )
            await self._synchronize_message_reactions(after)
            if not after.author.bot:
                await self.short_term_message_repository.delete_attachments(after.id)
                for attachment in after.attachments:
                    attachment_kind = self.attachment_use_cases.get_kind(attachment.content_type)
                    if attachment_kind is None:
                        continue
                    await self.short_term_message_repository.save_attachment(
                        StoredAttachmentInput(
                            id=attachment.id,
                            message_id=after.id,
                            url=attachment.url,
                            filename=attachment.filename,
                            content_type=attachment.content_type,
                            kind=attachment_kind,
                        )
                    )
                    self.attachment_use_cases.schedule(
                        after.id,
                        attachment.id,
                        attachment.filename,
                        attachment.url,
                        attachment_kind,
                    )

            if not after.author.bot:
                await self.long_term_memory_use_cases.enqueue(after.id, after.channel.id)

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
        is_unanswered_question: bool,
        custom_profile: CustomProfile | None,
    ) -> None:
        """同一チャンネルの返信要求を順番に生成し、保留メッセージを文脈へ反映します。"""
        current_message = message
        current_is_explicit_call = is_explicit_call
        current_is_unanswered_question = is_unanswered_question
        current_custom_profile = custom_profile
        completed_normally = False
        try:
            while True:
                now = asyncio.get_running_loop().time()
                if current_is_explicit_call or can_start_spontaneous_generation(state.last_spontaneous_action_at, now):
                    await self._generate_and_send_response(
                        current_message,
                        state,
                        is_explicit_call=current_is_explicit_call,
                        is_unanswered_question=current_is_unanswered_question,
                        custom_profile=current_custom_profile,
                    )
                else:
                    if await self._is_shadow_mode_for_message(
                        current_message,
                        is_explicit_call=current_is_explicit_call,
                    ):
                        await self._save_shadow_candidate(
                            current_message,
                            LLMMessage(action=ResponseAction.SILENCE, shadow_reason=ShadowReason.COOLDOWN),
                        )
                    logger.info(
                        "Skipped spontaneous chatbot generation due to cooldown (channel_id=%s)",
                        current_message.channel.id,
                    )

                async with state.lock:
                    await self._flush_pending_messages(state)
                    next_message = state.queued_response_message
                    next_is_explicit_call = state.queued_response_is_explicit_call
                    next_is_unanswered_question = state.queued_response_is_unanswered_question
                    next_custom_profile = state.queued_custom_profile
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    state.queued_response_is_unanswered_question = False
                    state.queued_custom_profile = None
                    if next_message is None:
                        state.generating = False
                        completed_normally = True
                        return
                    current_message = next_message
                    current_is_explicit_call = next_is_explicit_call
                    current_is_unanswered_question = next_is_unanswered_question
                    current_custom_profile = next_custom_profile
        finally:
            if not completed_normally:
                async with state.lock:
                    await self._flush_pending_messages(state)
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    state.queued_response_is_unanswered_question = False
                    state.queued_custom_profile = None
                    state.generating = False

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
            self.settings_use_cases.conversation_reset_minutes,
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
                )
            )
            await self._synchronize_message_reactions(message)
            if not message.author.bot:
                for attachment in message.attachments:
                    attachment_kind = self.attachment_use_cases.get_kind(attachment.content_type)
                    if attachment_kind is None:
                        continue
                    await self.short_term_message_repository.save_attachment(
                        StoredAttachmentInput(
                            id=attachment.id,
                            message_id=message.id,
                            url=attachment.url,
                            filename=attachment.filename,
                            content_type=attachment.content_type,
                            kind=attachment_kind,
                        )
                    )
                    self.attachment_use_cases.schedule(
                        message.id,
                        attachment.id,
                        attachment.filename,
                        attachment.url,
                        attachment_kind,
                    )
        if not message.author.bot:
            state.last_human_message_timestamp = message.created_at

    async def on_raw_reaction_change(self, message_id: int, channel_id: int) -> None:
        """リアクションイベントを応答開始条件にせず、短期文脈だけ更新します。"""
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
        is_unanswered_question: bool,
        custom_profile: CustomProfile | None,
    ) -> None:
        """短期記憶から回答を生成し、生成中に文脈が更新されていなければ送信します。"""
        generation_revision = state.generation_revision
        async with message.channel.typing():
            parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
            role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
            long_term_memory_context = await self.memory_search_use_cases.build_response_context(message.channel.id)
            generated_response = await self.response_pipelines[message.channel.id].generate_response(
                role,
                is_unanswered_question=is_unanswered_question,
                long_term_memory_context=long_term_memory_context,
                custom_profile=custom_profile,
            )

        async with state.lock:
            if not is_generation_current(state, generation_revision):
                return
            if await self._is_shadow_mode_for_message(message, is_explicit_call=is_explicit_call):
                await self._save_shadow_candidate(message, generated_response)
                return
            await self.execute_response_action(message, generated_response, state, is_explicit_call=is_explicit_call)

    async def execute_response_action(
        self,
        message: Message,
        response: LLMMessage,
        state: ChannelProcessingState,
        *,
        is_explicit_call: bool,
    ) -> None:
        """構造化された応答行動をDiscord上で実行します。"""
        if response.action is ResponseAction.SILENCE:
            return

        now = asyncio.get_running_loop().time()
        if not is_explicit_call and not can_execute_spontaneous_action(
            response.action,
            state.last_spontaneous_action_at,
            now,
        ):
            logger.info(
                "Skipped spontaneous chatbot action due to cooldown (action=%s, channel_id=%s)",
                response.action.value,
                message.channel.id,
            )
            return

        if response.action is ResponseAction.MESSAGE:
            await send_split_response(message.channel, response.content)
            if not is_explicit_call:
                state.last_spontaneous_action_at = now
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
            await target_message.add_reaction(reaction_emoji)
            if not is_explicit_call:
                state.last_spontaneous_action_at = now
            return

        await reply_with_split_response(target_message, response.content)
        if not is_explicit_call:
            state.last_spontaneous_action_at = now

    async def _is_shadow_mode_for_message(self, message: Message, *, is_explicit_call: bool) -> bool:
        """明示依頼以外のchat役割でシャドーモードを適用するか判定します。"""
        if is_explicit_call:
            return False
        parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
        return role is ChannelRole.CHAT and await self.shadow_mode_manager.is_enabled(message.channel.id)

    async def _save_shadow_candidate(self, message: Message, response: LLMMessage) -> None:
        """モデルが選んだ自発反応を、投稿せず評価用候補として保存します。"""
        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
        await self.shadow_candidate_repository.save(
            ShadowCandidateInput(
                channel_id=message.channel.id,
                trigger_message_id=message.id,
                action=response.action.value,
                reply_to_message_id=response.reply_to_message_id,
                content=response.content,
                reaction_emoji=response.reaction_emoji,
                reason=response.shadow_reason.value,
                context_message_ids=[memory_message.message_id for memory_message in short_term_memory.memory],
                context_snapshot=[memory_message.to_dict() for memory_message in short_term_memory.memory],
            )
        )
