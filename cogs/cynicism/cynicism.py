"""冷笑ランキングのDiscordイベント・コマンドの受付とライフサイクル管理。"""

from logging import getLogger

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.runtime_environment import get_runtime_environment

from .constants import REPORT_CHECK_INTERVAL_MINUTES
from .database import CynicismDatabase
from .periods import CynicismPeriodType
from .repositories.configuration import CynicismConfigurationRepository
from .repositories.reaction import CynicismReactionRepository
from .repositories.report import CynicismReportRepository
from .use_cases.reporting import CynicismReportUseCases
from .use_cases.tracking import CynicismTrackingUseCases

logger = getLogger(__name__)

PERIOD_CHOICES = [
    app_commands.Choice(name="週間", value=CynicismPeriodType.WEEKLY.value),
    app_commands.Choice(name="月間", value=CynicismPeriodType.MONTHLY.value),
    app_commands.Choice(name="年間", value=CynicismPeriodType.YEARLY.value),
]


class Cynicism(commands.Cog):
    """Discordイベントと冷笑ポイントのユースケースを接続するController。"""

    cynicism = app_commands.Group(name="cynicism", description="冷笑ポイントと冷笑王ランキングを扱います。")

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Botの接続とDBセッションを受け取り、記録・発表のユースケースを構成する。"""
        self._bot = bot
        self._runtime_environment = get_runtime_environment()
        self._database = CynicismDatabase(session_factory, self._runtime_environment.environment)
        reaction_repository = CynicismReactionRepository(self._database)
        configuration_repository = CynicismConfigurationRepository(self._database)
        report_repository = CynicismReportRepository(self._database)
        self._tracking_use_cases = CynicismTrackingUseCases(
            bot,
            self._runtime_environment,
            reaction_repository,
            configuration_repository,
        )
        self._report_use_cases = CynicismReportUseCases(
            bot,
            self._runtime_environment,
            reaction_repository,
            configuration_repository,
            report_repository,
        )

    async def cog_load(self) -> None:
        """実行環境に対応するスキーマへテーブルとインデックスを用意する。"""
        await self._database.initialize()

    async def cog_unload(self) -> None:
        """Cog終了時に発表確認ループを停止する。"""
        self.report_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """本番環境でだけ、週次・月次・年次の自動発表を開始する。"""
        if self._runtime_environment.is_debug:
            logger.info("Disabled scheduled cynicism reports in debug environment")
            return
        if not self.report_loop.is_running():
            self.report_loop.start()

    @tasks.loop(minutes=REPORT_CHECK_INTERVAL_MINUTES)
    async def report_loop(self) -> None:
        """発表時刻を過ぎた期間の投稿処理をユースケースへ委譲する。"""
        try:
            await self._report_use_cases.process_scheduled_reports()
        except Exception:
            logger.exception("Failed to process scheduled cynicism reports")

    @report_loop.before_loop
    async def before_report_loop(self) -> None:
        """Discord接続とテーブル作成が完了するまで待機する。"""
        await self._bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """リアクション追加イベントを、対象判定と記録処理へ渡す。"""
        await self._tracking_use_cases.on_reaction_add(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """リアクション削除イベントを、記録の取り消し処理へ渡す。"""
        await self._tracking_use_cases.on_reaction_remove(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        """全リアクションの削除を、対象メッセージの記録へ反映する。"""
        await self._tracking_use_cases.on_reaction_clear(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        """特定絵文字の一括削除を、対象リアクションの記録へ反映する。"""
        await self._tracking_use_cases.on_reaction_clear_emoji(payload)

    @cynicism.command(name="ranking", description="指定期間の冷笑王ランキングを表示します。")
    @app_commands.describe(period="集計期間の種別", start="期間内の任意の日 (YYYY-MM-DD、省略時は進行中の期間)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def ranking(
        self,
        interaction: Interaction,
        period: str = CynicismPeriodType.WEEKLY.value,
        start: str | None = None,
    ) -> None:
        """指定期間の順位を表示する。日付を省略した場合は進行中の期間を使う。"""
        await self._report_use_cases.ranking(interaction, period, start)

    @cynicism.command(name="messages", description="指定メンバーが冷笑ポイントを獲得した発言をEmbedで紹介します。")
    @app_commands.describe(
        member="対象メンバー",
        period="集計期間の種別",
        start="期間内の任意の日 (YYYY-MM-DD、省略時は進行中の期間)",
    )
    @app_commands.choices(period=PERIOD_CHOICES)
    async def messages(
        self,
        interaction: Interaction,
        member: discord.Member,
        period: str = CynicismPeriodType.WEEKLY.value,
        start: str | None = None,
    ) -> None:
        """指定メンバーの発言明細を、実行者だけにEmbedで紹介する。"""
        await self._report_use_cases.show_messages(interaction, member, period, start)

    @cynicism.command(name="status", description="冷笑ポイントの集計状態と発表状態を表示します。")
    async def status(self, interaction: Interaction) -> None:
        """集計の停止状態と、発表先・最終発表を表示する。"""
        await self._report_use_cases.status(interaction)

    @cynicism.command(name="publish", description="指定期間のランキングを発表チャンネルへ投稿または更新します。")
    @app_commands.describe(period="集計期間の種別", start="期間内の任意の日 (YYYY-MM-DD、省略時は直近の完了期間)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def publish(
        self,
        interaction: Interaction,
        period: str = CynicismPeriodType.WEEKLY.value,
        start: str | None = None,
    ) -> None:
        """管理者の操作で、指定期間の発表を投稿または更新する。"""
        await self._report_use_cases.publish(interaction, period, start)

    @cynicism.command(name="pause", description="冷笑ポイントの記録と自動発表を一時停止します。")
    async def pause(self, interaction: Interaction) -> None:
        """管理者の操作で、新規記録と自動発表を一時停止する。"""
        await self._report_use_cases.pause(interaction)

    @cynicism.command(name="resume", description="冷笑ポイントの記録と自動発表を再開します。")
    async def resume(self, interaction: Interaction) -> None:
        """管理者の操作で、新規記録と自動発表を再開する。"""
        await self._report_use_cases.resume(interaction)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """冷笑ポイントCogをBotへ登録する。"""
    await bot.add_cog(Cynicism(bot, session_factory))
    logger.debug("%s is added to the bot.", __name__)
