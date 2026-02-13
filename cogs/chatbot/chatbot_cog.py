import asyncio
import os
import random
from logging import getLogger

import discord
from discord import Message
from discord.ext import commands
from openai import AsyncOpenAI

from .responses_api import DraftGenerator, ResponseStyler, ShortTermMemory

logger = getLogger(__name__)


class ChatBot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = AsyncOpenAI()
        self.draft_generator = DraftGenerator(self.client)
        self.response_styler = ResponseStyler(self.client)
        self.target_channel: int = 0
        self.short_term_memory = ShortTermMemory()

        self._mem_lock = asyncio.Lock()
        self._generating = False
        self._pending: dict[int, discord.Message] = {}  # message_id -> Message（重複防止）
        self._last_caught_up_id: int | None = None  # 追いつき用のカーソル
        self._background_tasks: set[asyncio.Task[None]] = set()

        self.reply_probability = 0.15

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.channel.id != int(os.environ["TARGET_CHANNEL"]):
            return

        # 生成中なら memory を触らず pending に退避
        if self._generating:
            self._pending[message.id] = message
            return

        # 生成中でなければ普通に memory に追加
        async with self._mem_lock:
            await self.short_term_memory.append(message)
            self.short_term_memory.forget()
            self._last_caught_up_id = message.id

        if message.author.bot:
            return

        # 一定確率で応答（生成は on_message の外で回す）
        if random.random() < self.reply_probability and not self._generating:
            task = asyncio.create_task(self._generate_and_post(message.channel))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return

        for user in message.mentions:
            if user.id == self.bot.user.id:
                task = asyncio.create_task(self._generate_and_post(message.channel))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return

        if message.channel.id == int(os.environ["TARGET_CHANNEL"]):
            await self.short_term_memory.append(message)

    async def _generate_and_post(self, channel: discord.abc.Messageable) -> None:
        # 二重起動防止
        if self._generating:
            return

        self._generating = True

        try:
            # --- 1) 生成用スナップショットを作る（ここだけ lock）
            async with self._mem_lock:
                # pending が溜まっていたら、まずは取り込む（古い順）
                for mid in sorted(self._pending):
                    await self.short_term_memory.append(self._pending[mid])
                self._pending.clear()

                self.short_term_memory.forget()

                # cutoff（この時点までを入力に含めた、という境界）
                cutoff_id = self._last_caught_up_id

            logger.info("Generating response...")

            async with channel.typing():
                # --- 2) LLM生成（ここは lock しない：時間がかかるので）
                draft = await self.draft_generator.draft(
                    self.bot.user.display_name if self.bot.user else "", self.short_term_memory
                )
                final_response = await self.response_styler.style(
                    self.bot.user.display_name if self.bot.user else "", self.short_term_memory, draft
                )

                # --- 3) reply_to から対応するメッセージを検索
                if final_response.reply_to == "All":
                    await channel.send(final_response.content)
                    return

                reply_message = None

                if isinstance(channel, discord.TextChannel | discord.Thread):
                    # short_term_memory から reply_to に一致するメッセージの ID を探す
                    for mem_msg in reversed(self.short_term_memory.memory):
                        if mem_msg.author_name == final_response.reply_to:
                            try:
                                reply_message = await channel.fetch_message(mem_msg.message_id)
                                break
                            except discord.NotFound:
                                logger.warning(f"Message {mem_msg.message_id} not found in channel")
                                continue

                # --- 4) 投稿
                if reply_message:
                    # リプライメッセージとして送信
                    sent = await reply_message.reply(final_response.content)
                else:
                    # 対応するメッセージが見つからない場合は通常投稿
                    sent = await channel.send(final_response.content)

            # --- 5) 投稿後に「最新まで追いつく」
            # cutoff 以降のメッセージを history から取り込む（Bot投稿も含めるなら sent も append）
            async with self._mem_lock:
                # bot の投稿も memory に入れたいなら（必要なら reply_to なども整備）
                await self.short_term_memory.append(sent)  # append が Message を受け取れる前提
                # cutoff が None の場合（初回など）は after を使わず recent を取る運用でもOK
                if cutoff_id is not None and isinstance(channel, discord.TextChannel | discord.Thread):
                    after_obj = discord.Object(id=cutoff_id)
                    async for m in channel.history(after=after_obj, oldest_first=True, limit=200):
                        # 生成中に on_message で拾えなかった分や、取りこぼし対策
                        await self.short_term_memory.append(m)

                # pending に溜まっていたものも取り込む（保険）
                for mid in sorted(self._pending):
                    await self.short_term_memory.append(self._pending[mid])
                self._pending.clear()

                self.short_term_memory.forget()

                # 追いついた時点の最後を更新
                if isinstance(channel, discord.TextChannel | discord.Thread):
                    # history は取れない場合があるので、取れたものがあれば更新、なければ sent.id
                    self._last_caught_up_id = max(self._last_caught_up_id or 0, sent.id)

        finally:
            self._generating = False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatBot(bot))
