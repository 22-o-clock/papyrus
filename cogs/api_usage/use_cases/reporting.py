import asyncio
import datetime
import os
from logging import getLogger

from discord import Interaction, Member
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.api_usage.models import ReportMeasurementState
from cogs.api_usage.openai_usage import JST, OpenAIOrganizationUsageClient, OpenAIUsageSummary
from cogs.api_usage.repositories.report import ApiUsageReportDatabase
from cogs.api_usage.services.message_delivery import ApiUsageReportMessageDelivery, ReportMessageOwnershipError
from cogs.api_usage.services.report_builder import aggregate_feature_usages, build_usage_embed
from cogs.chatbot.observability import configure_chatbot_api_usage, get_measurement_error_count
from cogs.chatbot.repositories.api_usage import UTC
from core.exception.exception import ArgumentError, MissingRequiredRoleError
from core.runtime_environment import RuntimeEnvironment, get_runtime_environment

logger = getLogger(__name__)
OPENAI_RETRY_INTERVAL = datetime.timedelta(hours=1)
MAXIMUM_BACKFILL_DAYS = 7
MAXIMUM_HOUR = 23
MAXIMUM_MINUTE = 59


def should_run_daily_report(now: datetime.datetime, report_hour: int, report_minute: int) -> bool:
    """JSTの設定時刻を過ぎていれば当日の日次処理対象と判定する。"""
    local_now = now.astimezone(JST)
    return local_now.time().replace(tzinfo=None) >= datetime.time(report_hour, report_minute)


def validate_report_date(report_date: datetime.date, current_utc_date: datetime.date) -> None:
    """進行中の対象期間は許可し、未来の開始日だけを拒否する。"""
    if report_date > current_utc_date:
        message = "未来の対象期間開始日は指定できません。"
        raise ArgumentError(message)


class ApiUsageReportUseCases:
    """API利用レポートの手動操作と定期実行を調整する。"""

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._database = ApiUsageReportDatabase(session_factory)
        self._runtime_environment: RuntimeEnvironment = get_runtime_environment()
        self._usage_repository = configure_chatbot_api_usage(session_factory)
        self._admin_role_id = int(os.environ["ROLE_ID_BOT_ADMIN"])
        self._target_id = self._runtime_environment.api_usage_report_target_id
        self._client = OpenAIOrganizationUsageClient(
            os.environ["OPENAI_ADMIN_API_KEY"],
            project_id=os.environ["OPENAI_USAGE_PROJECT_ID"],
        )
        self._message_delivery = ApiUsageReportMessageDelivery(bot, self._database, self._target_id)
        self._report_lock = asyncio.Lock()
        self._last_daily_run: datetime.date | None = None
        self._last_openai_retry_at: datetime.datetime | None = None

    async def initialize(self) -> None:
        """計測開始日時を未作成の場合だけ初期化する。"""
        await self._usage_repository.initialize_measurement()

    async def process_scheduled_reports(self, now: datetime.datetime | None = None) -> None:
        """設定時刻後の欠損補完、前日投稿、前々日再集計を行う。"""
        if self._runtime_environment.is_debug:
            return
        current_time = now or datetime.datetime.now(JST)
        configuration = await self._database.get_configuration()
        if not should_run_daily_report(current_time, configuration.report_hour, configuration.report_minute):
            return
        async with self._report_lock:
            if self._last_daily_run != current_time.date():
                await self._run_daily_reports(current_time.astimezone(UTC).date())
                self._last_daily_run = current_time.date()
            await self._retry_unavailable_openai_cost(current_time)

    async def report(self, interaction: Interaction, date: str | None = None) -> None:
        """管理者操作で指定日のレポートを同じメッセージへ投稿・更新する。"""
        self._require_admin(interaction)
        report_date = self._parse_report_date(date)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self._report_lock:
                await self._post_or_update_report(report_date)
        except ReportMessageOwnershipError:
            logger.exception("Refused to overwrite another Bot's API usage report")
            await interaction.followup.send(
                "投稿先または配送記録が別のBotのAPI usageレポートを指しています。"
                "本番とデバッグで別の投稿先を設定してください。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"{report_date:%Y-%m-%d} 09:00 ~ "
            f"{report_date + datetime.timedelta(days=1):%Y-%m-%d} 09:00 JST のレポートを投稿または更新しました。",
            ephemeral=True,
        )

    async def schedule(self, interaction: Interaction, hour: int, minute: int) -> None:
        """管理者が毎日の投稿時刻をJSTで変更する。"""
        self._require_admin(interaction)
        if self._runtime_environment.is_debug:
            await interaction.response.send_message(
                "デバッグ環境では、本番と共有するAPI usage投稿時刻を変更できません。",
                ephemeral=True,
            )
            return
        if not 0 <= hour <= MAXIMUM_HOUR or not 0 <= minute <= MAXIMUM_MINUTE:
            message = "時は0-23、分は0-59で指定してください。"
            raise ArgumentError(message)
        await self._database.set_report_time(hour, minute)
        await interaction.response.send_message(
            f"投稿時刻を毎日 {hour:02d}:{minute:02d} JST に変更しました。",
            ephemeral=True,
        )

    async def status(self, interaction: Interaction) -> None:
        """現在時刻、投稿先、最終成功、次の対象日を管理者だけに表示する。"""
        self._require_admin(interaction)
        now = datetime.datetime.now(JST)
        configuration = await self._database.get_configuration()
        last_delivery = await self._database.get_last_delivery(self._target_id)
        next_date = now.astimezone(UTC).date() - datetime.timedelta(days=1)
        next_end_date = next_date + datetime.timedelta(days=1)
        last_text = (
            f"{last_delivery.report_date:%Y-%m-%d} ({last_delivery.last_updated_at.astimezone(JST):%Y-%m-%d %H:%M JST})"
            if last_delivery is not None
            else "なし"
        )
        environment_note = (
            "\n実行環境: debug (自動投稿停止、手動投稿のみ)" if self._runtime_environment.is_debug else "\n実行環境: production"
        )
        await interaction.response.send_message(
            f"現在: {now:%Y-%m-%d %H:%M JST}\n"
            f"投稿時刻: {configuration.report_hour:02d}:{configuration.report_minute:02d} JST\n"
            f"投稿先: <#{self._target_id}>\n最終成功: {last_text}\n"
            f"次の対象期間: {next_date:%Y-%m-%d} 09:00 ~ {next_end_date:%Y-%m-%d} 09:00 JST"
            f"{environment_note}",
            ephemeral=True,
        )

    async def _run_daily_reports(self, today: datetime.date) -> None:
        """最大7日内の欠損を古い順に補い、前々日を遅延請求反映のため更新する。"""
        started_at = await self._usage_repository.get_measurement_started_at()
        if started_at is None:
            return
        first_date = max(started_at.astimezone(UTC).date(), today - datetime.timedelta(days=MAXIMUM_BACKFILL_DAYS))
        yesterday = today - datetime.timedelta(days=1)
        yesterday_was_delivered = await self._database.has_delivery(yesterday, self._target_id)
        for offset in range((yesterday - first_date).days + 1):
            report_date = first_date + datetime.timedelta(days=offset)
            if not await self._database.has_delivery(report_date, self._target_id):
                await self._post_or_update_report(report_date)
        if yesterday >= first_date and yesterday_was_delivered:
            await self._post_or_update_report(yesterday)
        refresh_date = today - datetime.timedelta(days=2)
        if refresh_date >= first_date and await self._database.has_delivery(refresh_date, self._target_id):
            await self._post_or_update_report(refresh_date)

    async def _retry_unavailable_openai_cost(self, now: datetime.datetime) -> None:
        """前日レポートの確定額取得失敗を1時間に一度だけ再試行する。"""
        if self._last_openai_retry_at is not None and now - self._last_openai_retry_at < OPENAI_RETRY_INTERVAL:
            return
        report_date = now.astimezone(UTC).date() - datetime.timedelta(days=1)
        delivery = await self._database.get_delivery(report_date, self._target_id)
        if (
            delivery is not None
            and not delivery.openai_cost_available
            and now - delivery.last_updated_at.astimezone(JST) >= OPENAI_RETRY_INTERVAL
        ):
            self._last_openai_retry_at = now
            await self._post_or_update_report(report_date)

    async def _post_or_update_report(self, report_date: datetime.date) -> None:
        """指定日の集計を作り、既存投稿の編集または新規投稿を行う。"""
        rows = await self._usage_repository.list_for_date(report_date)
        feature_usages = aggregate_feature_usages(rows)
        summary = await self._fetch_openai_summary(report_date)
        started_at = await self._usage_repository.get_measurement_started_at()
        embed = build_usage_embed(
            report_date,
            feature_usages,
            summary,
            ReportMeasurementState(
                started_at=started_at,
                error_count=get_measurement_error_count(report_date),
                is_complete=report_date < datetime.datetime.now(UTC).date(),
            ),
        )
        message, posted_at = await self._message_delivery.upsert(report_date, embed)
        await self._database.save_delivery(
            report_date,
            self._target_id,
            message.id,
            posted_at=posted_at,
            openai_cost_available=summary is not None,
        )

    async def _fetch_openai_summary(self, report_date: datetime.date) -> OpenAIUsageSummary | None:
        """OpenAI側の障害時もローカル機能別レポートを継続できるよう失敗を分離する。"""
        try:
            return await self._client.fetch_daily_summary(report_date)
        except Exception:
            logger.exception("Failed to fetch OpenAI organization cost (report_date=%s)", report_date)
            return None

    def _parse_report_date(self, value: str | None) -> datetime.date:
        """省略時は前日、指定時はYYYY-MM-DDとして未来日を拒否する。"""
        if value is None:
            return datetime.datetime.now(UTC).date() - datetime.timedelta(days=1)
        try:
            report_date = datetime.date.fromisoformat(value)
        except ValueError as error:
            message = "日付は YYYY-MM-DD 形式で指定してください。"
            raise ArgumentError(message) from error
        validate_report_date(report_date, datetime.datetime.now(UTC).date())
        return report_date

    def _require_admin(self, interaction: Interaction) -> None:
        """Bot管理者ロールを持つ利用者だけに操作を許可する。"""
        if isinstance(interaction.user, Member) and any(role.id == self._admin_role_id for role in interaction.user.roles):
            return
        message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(message)
