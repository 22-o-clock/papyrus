import asyncio
import random
from logging import getLogger

import discord
from discord import Message, MessageReference, app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .channel_roles import ChannelRole, ChannelRoleManager
from .database_envs import DatabaseEnvManager
from .responses_api import ResponsePipeline

logger = getLogger(__name__)


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
    spontaneous_chat_reply: bool,
) -> bool:
    """チャンネル役割と呼びかけ方法から返信の要否を決定します。"""
    explicitly_called = mentioned_bot or replied_to_bot
    return explicitly_called or (role is ChannelRole.CHAT and spontaneous_chat_reply)


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.response_pipelines: dict[int, ResponsePipeline] = {}
        self.env_manager = DatabaseEnvManager(session_factory)
        self.channel_role_manager = ChannelRoleManager(self.env_manager)

        self._mem_lock = asyncio.Lock()
        self._generating = False
        self._pending: list[Message] = []
        self._background_tasks: set[asyncio.Task[None]] = set()

        self.reply_probability = 0.15

    async def initialize_response_pipeline_for_channel(self, channel_id: int) -> None:
        if self.bot.user:
            self.response_pipelines[channel_id] = ResponsePipeline(AsyncOpenAI(), self.bot.user.display_name)
        else:
            logger.warning(
                "Bot user is not available during initialization, response pipeline may not be initialized correctly"
            )
            self.response_pipelines[channel_id] = ResponsePipeline(AsyncOpenAI(), "Bot")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # chat役割のチャンネルで使用する自発返信確率を取得
        self.reply_probability = float(await self.env_manager.get_env("REPLY_PROBABILITY") or 0.15)

    @app_commands.command(
        description="chat役割のチャンネルでボットが自発返信する確率を変更します (0から1の間)",
    )
    async def change_reply_probability(self, interaction: discord.Interaction, probability: float) -> None:
        if not 0 <= probability <= 1:
            await interaction.response.send_message("確率は0から1の間で指定してください。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"ボットの返信確率を {self.reply_probability:.2f} から {probability:.2f} に変更しました。"
        )
        self.reply_probability = probability
        await self.env_manager.set_env("REPLY_PROBABILITY", str(probability))

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
        if message.channel.id not in self.response_pipelines:
            async with self._mem_lock:
                if message.channel.id not in self.response_pipelines:
                    await self.initialize_response_pipeline_for_channel(message.channel.id)

        # 2. 回答の生成中は memory を触らずメッセージを pending に退避して終了
        if self._generating:
            self._pending.append(message)
            return

        # 3. 回答が生成中でない場合の処理
        # 3.1 メッセージを memory に追加 (ここは lock する)
        async with self._mem_lock:
            await self.response_pipelines[message.channel.id].short_term_memory.append(message)

        # 3.2 回答を行うかの判定
        # 3.2.1 ボットのメッセージについては返信しない
        if message.author.bot:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        parent_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        role = await self.channel_role_manager.get_role(message.channel.id, parent_channel_id)
        mentioned_bot = any(user.id == bot_user.id for user in message.mentions)
        replied_to_bot = await self._is_reply_to_bot(message)
        spontaneous_chat_reply = role is ChannelRole.CHAT and random.SystemRandom().random() < self.reply_probability

        if should_respond(
            role,
            mentioned_bot=mentioned_bot,
            replied_to_bot=replied_to_bot,
            spontaneous_chat_reply=spontaneous_chat_reply,
        ):
            task = asyncio.create_task(self.reply_to_message(message))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

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

    async def reply_to_message(self, message: Message) -> None:
        # 1. 念のため generating フラグを確認して、生成中なら何もしないで終了
        if self._generating:
            return

        self._generating = True

        try:
            # 2. 念のため、生成前に pending に溜まっているメッセージを memory に取り込む (ここは lock する)
            async with self._mem_lock:
                for pending_message in sorted(self._pending, key=lambda m: m.id):
                    await self.response_pipelines[pending_message.channel.id].short_term_memory.append(pending_message)
                self._pending.clear()

            # 3. typing エフェクトを出しつつ、LLM で回答を生成
            async with message.channel.typing():
                generated_response = await self.response_pipelines[message.channel.id].generate_response()
                is_replied = False

                # 3.1 返信が生成された場合の処理
                # 3.1.1 モデルが短期記憶内のメッセージIDを指定した場合、そのメッセージに返信
                reply_to_message_id = generated_response.reply_to_message_id
                short_term_memory = self.response_pipelines[message.channel.id].short_term_memory
                if (
                    reply_to_message_id is not None
                    and short_term_memory.contains_message(reply_to_message_id)
                    and isinstance(message.channel, discord.TextChannel | discord.Thread)
                ):
                    target_message = message.channel.get_partial_message(reply_to_message_id)
                    await target_message.reply(generated_response.content)
                    is_replied = True

                # 3.1.2 見つからない場合は通常のメッセージとして送信
                if not is_replied:
                    await message.channel.send(generated_response.content)

            # 4. 生成中に投稿されたメッセージを memory に取り込む (ここは lock する)
            async with self._mem_lock:
                for pending_message in sorted(self._pending, key=lambda m: m.id):
                    await self.response_pipelines[pending_message.channel.id].short_term_memory.append(pending_message)
                self._pending.clear()

        finally:
            self._generating = False


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(ChatBot(bot, session_factory))
