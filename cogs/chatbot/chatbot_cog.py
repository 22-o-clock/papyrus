import asyncio
import random
from dataclasses import dataclass, field
from logging import getLogger

import discord
from discord import Message, MessageReference, app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .channel_roles import ChannelRole, ChannelRoleManager
from .database_envs import DatabaseEnvManager
from .responses_api import LLMMessage, ResponseAction, ResponsePipeline

logger = getLogger(__name__)

ASSISTANT_DEBOUNCE_SECONDS = 2.0
CHAT_DEBOUNCE_MIN_SECONDS = 5.0
CHAT_DEBOUNCE_MAX_SECONDS = 15.0
CHAT_TEXT_COOLDOWN_SECONDS = 15 * 60
CHAT_REACTION_COOLDOWN_SECONDS = 2 * 60


@dataclass
class ChannelProcessingState:
    """チャンネルごとの生成状態と生成中に受信したメッセージを保持します。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generating: bool = False
    pending_messages: list[Message] = field(default_factory=list)
    queued_response_message: Message | None = None
    queued_response_is_explicit_call: bool = False
    debounce_task: asyncio.Task[None] | None = None
    debounced_response_message: Message | None = None
    debounced_response_is_explicit_call: bool = False
    generation_revision: int = 0
    last_spontaneous_action_at: float | None = None


def claim_response_slot(
    state: ChannelProcessingState,
    message: Message,
    *,
    is_explicit_call: bool,
) -> bool:
    """生成枠を確保し、使用中の場合は次の返信対象としてメッセージを保持します。"""
    if state.generating:
        state.queued_response_message = message
        state.queued_response_is_explicit_call = is_explicit_call
        state.generation_revision += 1
        return False

    state.generating = True
    return True


def get_response_debounce_seconds(role: ChannelRole) -> float:
    """役割に応じた返信生成前の待機秒数を返します。"""
    if role is ChannelRole.ASSISTANT:
        return ASSISTANT_DEBOUNCE_SECONDS
    return random.SystemRandom().uniform(CHAT_DEBOUNCE_MIN_SECONDS, CHAT_DEBOUNCE_MAX_SECONDS)


def is_generation_current(state: ChannelProcessingState, revision: int) -> bool:
    """生成開始後に、回答を作り直す必要がある返信要求が追加されていないか確認します。"""
    return state.generation_revision == revision


def can_execute_spontaneous_action(
    action: ResponseAction,
    last_action_at: float | None,
    now: float,
) -> bool:
    """自発反応が行動別のクールダウンを過ぎているか判定します。"""
    if action is ResponseAction.SILENCE or last_action_at is None:
        return True

    cooldown_seconds = CHAT_REACTION_COOLDOWN_SECONDS if action is ResponseAction.REACTION else CHAT_TEXT_COOLDOWN_SECONDS
    return now - last_action_at >= cooldown_seconds


def can_start_spontaneous_generation(last_action_at: float | None, now: float) -> bool:
    """全ての自発行動が抑制される期間を避けて生成を始めるか判定します。"""
    if last_action_at is None:
        return True
    return now - last_action_at >= CHAT_REACTION_COOLDOWN_SECONDS


def can_change_channel_role(*, is_thread: bool, manage_channels: bool) -> bool:
    """スレッドでは全員、通常チャンネルでは管理権限を持つ人だけに変更を許可します。"""
    return is_thread or manage_channels


def get_available_referenced_author_id(reference: MessageReference) -> int | None:
    """追加のAPI取得なしで利用できる返信元メッセージの発言者IDを返します。"""
    if isinstance(reference.resolved, Message):
        return reference.resolved.author.id
    if reference.cached_message is not None:
        return reference.cached_message.author.id
    return None


def should_respond(
    role: ChannelRole,
    *,
    mentioned_bot: bool,
    replied_to_bot: bool,
) -> bool:
    """チャンネル役割と呼びかけ方法から、応答判断を開始するか決定します。"""
    explicitly_called = mentioned_bot or replied_to_bot
    return explicitly_called or role is ChannelRole.CHAT


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.response_pipelines: dict[int, ResponsePipeline] = {}
        self.env_manager = DatabaseEnvManager(session_factory)
        self.channel_role_manager = ChannelRoleManager(self.env_manager)

        self._initialization_lock = asyncio.Lock()
        self._channel_states: dict[int, ChannelProcessingState] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

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
                    self._channel_states[channel_id] = ChannelProcessingState()
        return self._channel_states[channel_id]

    @app_commands.command(name="show_chatbot_role", description="このチャンネルでのChatbotの役割を表示します")
    async def show_chatbot_role(self, interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return

        is_thread = isinstance(interaction.channel, discord.Thread)
        parent_channel_id = interaction.channel.parent_id if is_thread else None
        configured_role = await self.channel_role_manager.get_configured_role(channel_id)
        role = await self.channel_role_manager.get_role(channel_id, parent_channel_id)
        source = "このスレッド固有の設定" if configured_role is not None else "親チャンネルからの継承"
        if not is_thread:
            source = "このチャンネルの設定" if configured_role is not None else "既定値"
        await interaction.response.send_message(
            f"このチャンネルのChatbotの役割は `{role.value}` です。設定元: {source}。",
            ephemeral=True,
        )

    @app_commands.command(name="set_chatbot_role", description="このチャンネルでのChatbotの役割を変更します")
    @app_commands.describe(role="assistant または chat を選択します")
    async def set_chatbot_role(self, interaction: discord.Interaction, role: ChannelRole) -> None:
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return

        is_thread = isinstance(interaction.channel, discord.Thread)
        if not can_change_channel_role(
            is_thread=is_thread,
            manage_channels=interaction.permissions.manage_channels,
        ):
            await interaction.response.send_message(
                "通常チャンネルの役割変更には「チャンネルの管理」権限が必要です。",
                ephemeral=True,
            )
            return

        await self.channel_role_manager.set_role(channel_id, role)
        target_name = "スレッド" if is_thread else "チャンネル"
        await interaction.response.send_message(
            f"{interaction.user.mention} がこの{target_name}のChatbotの役割を `{role.value}` に変更しました。"
        )

    @app_commands.command(name="reset_chatbot_role", description="このチャンネル固有のChatbot役割を解除します")
    async def reset_chatbot_role(self, interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return

        is_thread = isinstance(interaction.channel, discord.Thread)
        if not can_change_channel_role(
            is_thread=is_thread,
            manage_channels=interaction.permissions.manage_channels,
        ):
            await interaction.response.send_message(
                "通常チャンネルの役割変更には「チャンネルの管理」権限が必要です。",
                ephemeral=True,
            )
            return

        await self.channel_role_manager.clear_role(channel_id)
        role = await self.channel_role_manager.get_role(
            channel_id,
            interaction.channel.parent_id if is_thread else None,
        )
        target_name = "スレッド" if is_thread else "チャンネル"
        source = "親チャンネルから継承" if is_thread else "既定値を使用"
        await interaction.response.send_message(
            f"{interaction.user.mention} がこの{target_name}固有の設定を解除しました。"
            f"現在は `{role.value}` です。設定元: {source}。"
        )

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        # 1. 明示的に呼ばれる前の会話も保持するため、チャンネルごとにパイプラインを遅延初期化
        state = await self._ensure_channel_state(message.channel.id)

        # 2. 同じチャンネルで回答を生成中の場合、生成中の文脈を変えないようメッセージを保留
        async with state.lock:
            if state.generating:
                state.pending_messages.append(message)
            else:
                await self.response_pipelines[message.channel.id].short_term_memory.append(message)

        # 3. 回答を行うかの判定
        # 3.1 ボットのメッセージについては返信しない
        if message.author.bot:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
        mentioned_bot = any(user.id == bot_user.id for user in message.mentions)
        replied_to_bot = await self._is_reply_to_bot(message)
        is_explicit_call = mentioned_bot or replied_to_bot
        response_required = should_respond(
            role,
            mentioned_bot=mentioned_bot,
            replied_to_bot=replied_to_bot,
        )
        await self._update_response_schedule(
            message if response_required else None,
            state,
            role,
            is_explicit_call=is_explicit_call,
        )

    async def _update_response_schedule(
        self,
        response_message: Message | None,
        state: ChannelProcessingState,
        role: ChannelRole,
        *,
        is_explicit_call: bool,
    ) -> None:
        """返信対象を更新し、最後の人間投稿から一定時間後に生成を開始します。"""
        async with state.lock:
            if state.generating:
                if response_message is not None:
                    claim_response_slot(state, response_message, is_explicit_call=is_explicit_call)
                return

            if response_message is not None:
                state.debounced_response_message = response_message
                state.debounced_response_is_explicit_call = is_explicit_call
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
            state.debounce_task = None
            state.debounced_response_message = None
            state.debounced_response_is_explicit_call = False
            if message is None or not claim_response_slot(state, message, is_explicit_call=is_explicit_call):
                return

        await self._process_response_queue(message, state, is_explicit_call=is_explicit_call)

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
    ) -> None:
        """同一チャンネルの返信要求を順番に生成し、保留メッセージを文脈へ反映します。"""
        current_message = message
        current_is_explicit_call = is_explicit_call
        completed_normally = False
        try:
            while True:
                now = asyncio.get_running_loop().time()
                if current_is_explicit_call or can_start_spontaneous_generation(state.last_spontaneous_action_at, now):
                    await self._generate_and_send_response(
                        current_message,
                        state,
                        is_explicit_call=current_is_explicit_call,
                    )
                else:
                    logger.info(
                        "Skipped spontaneous chatbot generation due to cooldown (channel_id=%s)",
                        current_message.channel.id,
                    )

                async with state.lock:
                    await self._flush_pending_messages(current_message.channel.id, state)
                    next_message = state.queued_response_message
                    next_is_explicit_call = state.queued_response_is_explicit_call
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    if next_message is None:
                        state.generating = False
                        completed_normally = True
                        return
                    current_message = next_message
                    current_is_explicit_call = next_is_explicit_call
        finally:
            if not completed_normally:
                async with state.lock:
                    await self._flush_pending_messages(message.channel.id, state)
                    state.queued_response_message = None
                    state.queued_response_is_explicit_call = False
                    state.generating = False

    async def _flush_pending_messages(self, channel_id: int, state: ChannelProcessingState) -> None:
        """生成中に保留したメッセージを時系列順で短期記憶へ移します。"""
        for pending_message in sorted(state.pending_messages, key=lambda pending: pending.id):
            await self.response_pipelines[channel_id].short_term_memory.append(pending_message)
        state.pending_messages.clear()

    async def _generate_and_send_response(
        self,
        message: Message,
        state: ChannelProcessingState,
        *,
        is_explicit_call: bool,
    ) -> None:
        """短期記憶から回答を生成し、生成中に文脈が更新されていなければ送信します。"""
        generation_revision = state.generation_revision
        async with message.channel.typing():
            parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
            role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
            generated_response = await self.response_pipelines[message.channel.id].generate_response(role)

        async with state.lock:
            if not is_generation_current(state, generation_revision):
                return
            await self._execute_response_action(
                message,
                generated_response,
                state,
                is_explicit_call=is_explicit_call,
            )

    async def _execute_response_action(
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
            await message.channel.send(response.content)
            if not is_explicit_call:
                state.last_spontaneous_action_at = now
            return

        reply_to_message_id = response.reply_to_message_id
        short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
        if (
            reply_to_message_id is None
            or not short_term_memory.contains_message(reply_to_message_id)
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

        await target_message.reply(response.content)
        if not is_explicit_call:
            state.last_spontaneous_action_at = now


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(ChatBot(bot, session_factory))
