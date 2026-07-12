import asyncio
from logging import getLogger
from random import SystemRandom

import discord
from discord import Message, app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database_envs import DatabaseEnvManager
from .responses_api import ResponsePipeline

logger = getLogger(__name__)
RANDOM = SystemRandom()


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.response_pipelines: dict[int, ResponsePipeline] = {}
        self.env_manager = DatabaseEnvManager(session_factory)

        self._mem_lock = asyncio.Lock()
        self._generating = False
        self._pending: list[Message] = []
        self._background_tasks: set[asyncio.Task[None]] = set()

        self.reply_probability = 0.15
        self.target_channel_list: list[int] = []

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
        # データベースからターゲットチャンネルIDのリストを取得
        target_channel_ids_str = await self.env_manager.get_env("TARGET_CHANNEL_IDS")
        if target_channel_ids_str:
            self.target_channel_list = [int(cid) for cid in target_channel_ids_str.split(",")]

        # 各ターゲットチャンネルに対して ResponsePipeline を初期化
        for channel_id in self.target_channel_list:
            await self.initialize_response_pipeline_for_channel(channel_id)

        # ボットの返信確率を取得
        self.reply_probability = float(await self.env_manager.get_env("REPLY_PROBABILITY") or 0.15)

    @app_commands.command(
        description="ボットが返信する確率を変更します (0から1の間)",
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

    @app_commands.command(
        description="コマンドが呼ばれたチャンネルをボットの返信対象チャンネルに追加します",
    )
    async def add_target_channel(self, interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id

        if channel_id in self.target_channel_list:
            await interaction.response.send_message("このチャンネルはすでに返信対象に含まれています。", ephemeral=True)
            return

        if channel_id is None:
            await interaction.response.send_message("チャンネル情報が取得できませんでした。", ephemeral=True)
            return

        # 途中で on_ready が呼ばれないように lock してからリストに追加し、ResponsePipeline を初期化する
        async with self._mem_lock:
            self.target_channel_list.append(channel_id)
            await self.initialize_response_pipeline_for_channel(channel_id)

        await self.env_manager.set_env("TARGET_CHANNEL_IDS", ",".join(str(cid) for cid in self.target_channel_list))
        await interaction.response.send_message(
            f"このチャンネルを返信対象に追加しました。現在の対象チャンネル数: {len(self.target_channel_list)}"
        )

    @app_commands.command(
        description="コマンドが呼ばれたチャンネルをボットの返信対象チャンネルから削除します",
    )
    async def remove_target_channel(self, interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id

        if channel_id not in self.target_channel_list:
            await interaction.response.send_message("このチャンネルは返信対象に含まれていません。", ephemeral=True)
            return

        self.target_channel_list.remove(channel_id)
        await self.env_manager.set_env("TARGET_CHANNEL_IDS", ",".join(str(cid) for cid in self.target_channel_list))
        await interaction.response.send_message(
            f"このチャンネルを返信対象から削除しました。現在の対象チャンネル数: {len(self.target_channel_list)}"
        )

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        # 1. 対象チャンネル以外に対するメッセージは無視
        if message.channel.id not in self.target_channel_list:
            return

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

        # 3.2.2 reply_probability に基づいて返信するかを決定
        if RANDOM.random() < self.reply_probability and not self._generating:
            task = asyncio.create_task(self.reply_to_message(message))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return

        # 3.2.3 メンションがあった場合は必ず返信
        for user in message.mentions:
            if self.bot.user and user.id == self.bot.user.id and not self._generating:
                task = asyncio.create_task(self.reply_to_message(message))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return

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
                # 3.1.1 short_term_memory から宛先のユーザーによる最新のメッセージが見つかれば、そのメッセージに返信
                if generated_response.reply_to != "All":
                    for message_in_memory in reversed(self.response_pipelines[message.channel.id].short_term_memory.memory):
                        if message_in_memory.author_name == generated_response.reply_to and isinstance(
                            message.channel, discord.TextChannel | discord.Thread
                        ):
                            target_message = message.channel.get_partial_message(message_in_memory.message_id)
                            await target_message.reply(generated_response.content)
                            is_replied = True
                            break

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
