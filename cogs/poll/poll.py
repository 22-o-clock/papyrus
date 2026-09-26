"""秘密投票のDiscordコマンド・ボタンの受付とライフサイクル管理。"""

import os
from logging import getLogger

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.runtime_environment import get_runtime_environment

from .constants import CHECK_INTERVAL_MINUTES, DEFAULT_DURATION
from .database import PollDatabase
from .repositories.poll import PollRepository
from .use_cases.polls import PollUseCases
from .views import POLL_COG_NAME, PollAction, PollButton

logger = getLogger(__name__)

CHANGEABLE_MODE = "changeable"
FIXED_MODE = "fixed"
MODE_CHOICES = [
    app_commands.Choice(name="変更可(既定): 締め切りまで変更・取り消しできます", value=CHANGEABLE_MODE),
    app_commands.Choice(name="変更不可: 投票後は変更できませんが、運営者にも投票内容が分かりません", value=FIXED_MODE),
]


class Poll(commands.Cog, name=POLL_COG_NAME):
    """Discordの操作と秘密投票のユースケースを接続するController。"""

    poll = app_commands.Group(name="poll", description="秘密投票を扱います。", guild_only=True)

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Botの接続とDBセッションを受け取り、秘密投票のユースケースを構成する。"""
        self._bot = bot
        self._database = PollDatabase(session_factory, get_runtime_environment().environment)
        self._use_cases = PollUseCases(
            bot,
            PollRepository(self._database),
            ballot_secret=os.environ["POLL_BALLOT_SECRET"].strip().encode(),
            electorate_role_id=int(os.environ["ROLE_ID_ELECTORATE"]),
        )

    async def cog_load(self) -> None:
        """テーブルを用意し、再起動前に投稿した告知メッセージのボタンを受け付ける。"""
        await self._database.initialize()
        self._bot.add_dynamic_items(PollButton)

    async def cog_unload(self) -> None:
        """締め切り確認ループを停止し、ボタンの受付を解除する。"""
        self.due_loop.cancel()
        self._bot.remove_dynamic_items(PollButton)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """締め切りと確定の確認を開始する。保存先が環境ごとに分かれているため、debug環境でも動かす。"""
        if not self.due_loop.is_running():
            self.due_loop.start()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def due_loop(self) -> None:
        """締め切り時刻や確定時刻を過ぎた投票の処理をユースケースへ委譲する。"""
        try:
            await self._use_cases.process_due_polls()
        except Exception:
            logger.exception("Failed to process due polls")

    @due_loop.before_loop
    async def before_due_loop(self) -> None:
        """Discord接続が完了するまで待機する。"""
        await self._bot.wait_until_ready()

    async def handle_poll_button(self, interaction: Interaction, poll_id: int, action: PollAction) -> None:
        """告知メッセージのボタン操作をユースケースへ渡す。"""
        await self._use_cases.handle_button(interaction, poll_id, action)

    @poll.command(name="create", description="秘密投票を作成します。質問と選択肢は、この後のフォームで入力します。")
    @app_commands.describe(
        duration="投票期間 (例: 24h、1d12h、30m、09/30 21:00。省略時は24時間)",
        multiple="複数の選択肢を選べるようにするか (省略時は単一選択)",
        role="投票できる人を絞り込むロール (有権者ロールは常に必須。@everyone は不可)",
        mode="変更不可にすると、投票後の変更・取り消しはできませんが、運営者にも投票内容が分からなくなります",
    )
    @app_commands.choices(mode=MODE_CHOICES)
    async def create(
        self,
        interaction: Interaction,
        duration: str = DEFAULT_DURATION,
        multiple: bool = False,  # noqa: FBT001, FBT002 - スラッシュコマンドの真偽値オプションのため。
        role: discord.Role | None = None,
        mode: str = CHANGEABLE_MODE,
    ) -> None:
        """設定を検証し、質問と選択肢の入力フォームを表示する。"""
        await self._use_cases.start_create(
            interaction,
            duration=duration,
            allow_multiple=multiple,
            allow_change=mode != FIXED_MODE,
            role=role,
        )

    @poll.command(name="electorate", description="投票の作成時点で確定した有権者リストを表示します。")
    @app_commands.describe(poll="投票を一覧から選択、またはIDを指定")
    async def electorate(self, interaction: Interaction, poll: str) -> None:
        """全メンバーに、有権者リストの確認を許可する。"""
        await self._use_cases.show_electorate(interaction, poll)

    @electorate.autocomplete("poll")
    async def electorate_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        """有権者リストを表示する投票の候補を返す。"""
        return await self._use_cases.autocomplete_polls(interaction, current)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """秘密投票CogをBotへ登録する。"""
    await bot.add_cog(Poll(bot, session_factory))
    logger.debug("%s is added to the bot.", __name__)
