from logging import getLogger

from discord import Interaction, app_commands
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .use_cases.reporting import ApiUsageReportUseCases

logger = getLogger(__name__)
REPORT_CHECK_INTERVAL_MINUTES = 1


class ApiUsageReporter(commands.Cog):
    """DiscordイベントとAPI利用レポートのユースケースを接続するController。"""

    api_usage = app_commands.Group(name="api_usage", description="Chatbot API日次レポートを管理します。")

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._use_cases = ApiUsageReportUseCases(bot, session_factory)

    async def cog_unload(self) -> None:
        """Cog終了時に日次確認ループを停止する。"""
        self.report_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """DB初期化完了後に計測開始日時を確定して日次ループを開始する。"""
        await self._use_cases.initialize()
        if not self.report_loop.is_running():
            self.report_loop.start()

    @tasks.loop(minutes=REPORT_CHECK_INTERVAL_MINUTES)
    async def report_loop(self) -> None:
        """設定時刻後の日次レポート処理をユースケースへ委譲する。"""
        try:
            await self._use_cases.process_scheduled_reports()
        except Exception:
            logger.exception("Failed to process daily Chatbot API usage reports")

    @report_loop.before_loop
    async def before_report_loop(self) -> None:
        """Discord接続とDBテーブル作成が完了するまで待機する。"""
        await self._bot.wait_until_ready()

    @api_usage.command(name="report", description="指定日のAPI利用レポートを投稿または更新します。")
    @app_commands.describe(date="対象期間の開始日 (JST 09:00開始、YYYY-MM-DD)")
    async def report(self, interaction: Interaction, date: str | None = None) -> None:
        await self._use_cases.report(interaction, date)

    @api_usage.command(name="schedule", description="API日次レポートの投稿時刻を変更します。")
    @app_commands.describe(hour="JSTの時 (0-23)", minute="分 (0-59)")
    async def schedule(self, interaction: Interaction, hour: int, minute: int) -> None:
        await self._use_cases.schedule(interaction, hour, minute)

    @api_usage.command(name="status", description="API日次レポートの現在の設定と状態を表示します。")
    async def status(self, interaction: Interaction) -> None:
        await self._use_cases.status(interaction)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """API利用レポートCogをBotへ登録する。"""
    await bot.add_cog(ApiUsageReporter(bot, session_factory))
    logger.debug("%s is added to the bot.", __name__)
