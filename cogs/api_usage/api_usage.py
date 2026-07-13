import asyncio
import datetime
import os
from dataclasses import dataclass, field
from decimal import Decimal
from logging import getLogger

import discord
from discord import Interaction, Member, TextChannel, Thread, app_commands
from discord.ext import commands, tasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.observability import configure_chatbot_api_usage, get_measurement_error_count
from cogs.chatbot.repositories.api_usage import UTC, ChatbotApiUsageDaily
from core.exception.exception import ArgumentError, MissingRequiredRoleError

from .database import ApiUsageReportDatabase, ReportDelivery
from .openai_usage import JST, OpenAIOrganizationUsageClient, OpenAIUsageSummary
from .pricing import PRICING_VERIFIED_ON, estimate_usage_cost

logger = getLogger(__name__)
REPORT_CHECK_INTERVAL_MINUTES = 1
OPENAI_RETRY_INTERVAL = datetime.timedelta(hours=1)
MAXIMUM_BACKFILL_DAYS = 7
MAXIMUM_HOUR = 23
MAXIMUM_MINUTE = 59
REPORT_MARKER_PREFIX = "api-usage-report:"
MEMORY_OPERATIONS = {
    "memory_extraction",
    "memory_reconciliation",
    "memory_embedding",
    "memory_search_embedding",
    "memory_admin_embedding",
}
FEATURE_LABELS = {
    "draft_generation": "応答生成・行動判断",
    "attachment_analysis": "添付ファイル解析",
    "memory_extraction": "長期記憶の抽出",
    "memory_reconciliation": "長期記憶の整合判定",
    "memory_embedding": "長期記憶の登録用Embedding",
    "memory_search_embedding": "長期記憶の検索用Embedding",
    "memory_admin_embedding": "管理更新用Embedding",
}
ITEM_LABELS = {
    "draft_generation": "応答",
    "attachment_analysis": "添付",
    "memory_extraction": "メッセージ",
    "memory_reconciliation": "判定",
    "memory_embedding": "記憶",
    "memory_search_embedding": "検索クエリ",
    "memory_admin_embedding": "記憶",
}


@dataclass(slots=True)
class FeatureUsage:
    """複数モデル行を機能単位にまとめた表示用集約。"""

    operation: str
    success_count: int = 0
    failure_count: int = 0
    item_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    web_search_calls: int = 0
    code_interpreter_sessions: int = 0
    estimated_cost: Decimal = field(default_factory=Decimal)
    model_cost: Decimal = field(default_factory=Decimal)
    web_search_cost: Decimal = field(default_factory=Decimal)
    code_interpreter_cost: Decimal = field(default_factory=Decimal)
    models: set[str] = field(default_factory=set)
    unknown_price_models: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ReportMeasurementState:
    """レポート期間の計測開始・保存失敗・完了状態。"""

    started_at: datetime.datetime | None
    error_count: int
    is_complete: bool = True


def should_run_daily_report(now: datetime.datetime, report_hour: int, report_minute: int) -> bool:
    """JSTの設定時刻を過ぎていれば当日の日次処理対象と判定する。"""
    local_now = now.astimezone(JST)
    return local_now.time().replace(tzinfo=None) >= datetime.time(report_hour, report_minute)


def validate_report_date(report_date: datetime.date, current_utc_date: datetime.date) -> None:
    """進行中の対象期間は許可し、未来の開始日だけを拒否する。"""
    if report_date > current_utc_date:
        message = "未来の対象期間開始日は指定できません。"
        raise ArgumentError(message)


def aggregate_feature_usages(rows: list[ChatbotApiUsageDaily]) -> list[FeatureUsage]:
    """モデル別DB行を、表示するChatbot機能単位へ集約する。"""
    usages: dict[str, FeatureUsage] = {}
    for row in rows:
        usage = usages.setdefault(row.operation, FeatureUsage(operation=row.operation))
        cost = estimate_usage_cost(row)
        usage.success_count += row.success_count
        usage.failure_count += row.failure_count
        usage.item_count += row.item_count
        usage.input_tokens += row.input_tokens
        usage.cached_input_tokens += row.cached_input_tokens
        usage.output_tokens += row.output_tokens
        usage.web_search_calls += row.web_search_calls
        usage.code_interpreter_sessions += row.code_interpreter_sessions
        usage.estimated_cost += cost.total
        usage.model_cost += cost.model_cost
        usage.web_search_cost += cost.web_search_cost
        usage.code_interpreter_cost += cost.code_interpreter_cost
        usage.models.add(row.model)
        if not cost.price_known:
            usage.unknown_price_models.add(row.model)
    return sorted(usages.values(), key=lambda item: item.estimated_cost, reverse=True)


def build_usage_embed(
    report_date: datetime.date,
    feature_usages: list[FeatureUsage],
    openai_summary: OpenAIUsageSummary | None,
    measurement_state: ReportMeasurementState,
) -> discord.Embed:
    """目的別の呼出量と推定コストを優先した日次Embedを作る。"""
    estimated_total = sum((usage.estimated_cost for usage in feature_usages), start=Decimal())
    exact_total = openai_summary.total_cost if openai_summary is not None else None
    embed = discord.Embed(
        title="Chatbot API 日次レポート",
        description=(
            f"**対象期間: {report_date:%Y-%m-%d} 09:00 ~ {report_date + datetime.timedelta(days=1):%Y-%m-%d} 09:00 JST**"
        ),
        colour=discord.Colour.blurple(),
        timestamp=datetime.datetime.now(JST),
    )
    embed.add_field(
        name="コスト概要",
        value=_format_cost_summary(
            exact_total,
            estimated_total,
            measurement_state.started_at,
            report_date,
            report_is_complete=measurement_state.is_complete,
        ),
        inline=False,
    )

    standalone = [usage for usage in feature_usages if usage.operation not in MEMORY_OPERATIONS]
    memory_usages = [usage for usage in feature_usages if usage.operation in MEMORY_OPERATIONS]
    display_groups: list[tuple[Decimal, str, FeatureUsage | list[FeatureUsage]]] = [
        (usage.estimated_cost, FEATURE_LABELS.get(usage.operation, usage.operation), usage) for usage in standalone
    ]
    if memory_usages:
        display_groups.append(
            (sum((usage.estimated_cost for usage in memory_usages), start=Decimal()), "長期記憶 合計", memory_usages)
        )
    for _, label, value in sorted(display_groups, key=lambda item: item[0], reverse=True):
        if isinstance(value, list):
            embed.add_field(
                name=f"{label} — {_format_usd(sum((u.estimated_cost for u in value), Decimal()))}",
                value=_format_memory_total(value),
                inline=False,
            )
            for usage in value:
                embed.add_field(
                    name=f"└ {FEATURE_LABELS[usage.operation]} — {_format_usd(usage.estimated_cost)}",
                    value=_format_feature_usage(usage, estimated_total),
                    inline=False,
                )
        else:
            embed.add_field(
                name=f"{label} — {_format_usd(value.estimated_cost)}",
                value=_format_feature_usage(value, estimated_total),
                inline=False,
            )

    unused = [label for operation, label in FEATURE_LABELS.items() if operation not in {u.operation for u in feature_usages}]
    if unused:
        embed.add_field(name="利用なし", value=" / ".join(unused), inline=False)
    warnings = _build_warnings(feature_usages, exact_total, estimated_total, measurement_state.error_count)
    if warnings:
        embed.add_field(name="⚠ 確認事項", value="\n".join(warnings), inline=False)
    embed.set_footer(
        text=(
            f"{REPORT_MARKER_PREFIX}{report_date.isoformat()} | cached input は input token の内数 | "
            f"cache write料金は未配賦 | 単価確認 {PRICING_VERIFIED_ON:%Y-%m-%d}"
        )
    )
    return embed


def _format_cost_summary(
    exact_total: Decimal | None,
    estimated_total: Decimal,
    measurement_started_at: datetime.datetime | None,
    report_date: datetime.date,
    *,
    report_is_complete: bool,
) -> str:
    """確定額・機能別推定・差額を短い概要にする。"""
    exact_text = _format_usd(exact_total) if exact_total is not None else "取得できませんでした (後で再試行)"
    openai_label = "OpenAI確定額" if report_is_complete else "OpenAI集計額 (暫定)"
    lines = [f"{openai_label}: **{exact_text}**", f"機能別推定: **{_format_usd(estimated_total)}**"]
    if exact_total is not None:
        lines.append(f"未配賦・差額: **{_format_signed_usd(exact_total - estimated_total)}**")
    if measurement_started_at is not None and measurement_started_at.astimezone(UTC).date() == report_date:
        lines.append(f"※ 機能別計測は {measurement_started_at.astimezone(JST):%Y-%m-%d %H:%M:%S JST} からの部分集計です。")
    if not report_is_complete:
        lines.append("※ 対象期間は進行中です。完了後の自動処理で同じ投稿を更新します。")
    return "\n".join(lines)


def _format_feature_usage(usage: FeatureUsage, estimated_total: Decimal) -> str:
    """機能単位の回数・token・ツール費を3行以内に整形する。"""
    share = usage.estimated_cost / estimated_total * 100 if estimated_total else Decimal()
    item_label = ITEM_LABELS.get(usage.operation, "対象")
    lines = [
        (
            f"成功 **{usage.success_count:,} calls** / 失敗 {usage.failure_count:,} calls / "
            f"{item_label} {usage.item_count:,}件 / {share:.1f}%"
        ),
        (
            f"input {usage.input_tokens:,} tokens (cached {usage.cached_input_tokens:,} tokens) / "
            f"output {usage.output_tokens:,} tokens"
        ),
        f"model tokens {_format_usd(usage.model_cost)} — {', '.join(sorted(usage.models))}",
    ]
    if usage.web_search_calls:
        lines.append(f"Web Search {usage.web_search_calls:,} calls {_format_usd(usage.web_search_cost)}")
    if usage.code_interpreter_sessions:
        lines.append(
            f"Code Interpreter {usage.code_interpreter_sessions:,} sessions {_format_usd(usage.code_interpreter_cost)}"
        )
    return "\n".join(lines)


def _format_memory_total(usages: list[FeatureUsage]) -> str:
    """長期記憶関連の合計値を詳細内訳の前に表示する。"""
    return (
        f"成功 **{sum(u.success_count for u in usages):,} calls** / 失敗 {sum(u.failure_count for u in usages):,} calls\n"
        f"input {sum(u.input_tokens for u in usages):,} tokens / output {sum(u.output_tokens for u in usages):,} tokens"
    )


def _build_warnings(
    usages: list[FeatureUsage],
    exact_total: Decimal | None,
    estimated_total: Decimal,
    measurement_error_count: int,
) -> list[str]:
    """単価不足・大きな差額・計測保存失敗だけを警告する。"""
    warnings: list[str] = []
    unknown_models = sorted({model for usage in usages for model in usage.unknown_price_models})
    if unknown_models:
        warnings.append(f"単価未登録モデル: {', '.join(unknown_models)} (該当model token費は推定額に未反映)")
    if exact_total is not None and exact_total:
        difference = abs(exact_total - estimated_total)
        if difference > Decimal("0.05") and difference / exact_total > Decimal("0.10"):
            warnings.append(f"確定額との差が {_format_usd(difference)} ({difference / exact_total * 100:.1f}%) あります。")
    if measurement_error_count:
        warnings.append(f"計測DBへの保存失敗を {measurement_error_count:,} calls 検出しました。")
    return warnings


def _format_usd(amount: Decimal | None) -> str:
    """USDを小数3桁で、非ゼロの極小額だけ不等号付きで表示する。"""
    if amount is None:
        return "—"
    if amount != 0 and abs(amount) < Decimal("0.001"):
        return "<$0.001" if amount > 0 else ">-$0.001"
    return f"${amount:.3f}"


def _format_signed_usd(amount: Decimal) -> str:
    """差額を符号付きUSD小数3桁で表示する。"""
    if amount != 0 and abs(amount) < Decimal("0.001"):
        return "+<$0.001" if amount > 0 else "->-$0.001"
    return f"{amount:+.3f} USD"


class ApiUsageReporter(commands.Cog):
    """OpenAI APIの機能別利用量とコストを毎日Discordへ投稿する。"""

    api_usage = app_commands.Group(name="api_usage", description="Chatbot API日次レポートを管理します。")

    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.db = ApiUsageReportDatabase(session_factory)
        self.usage_repository = configure_chatbot_api_usage(session_factory)
        self.admin_role_id = int(os.environ["BOT_ADMIN"])
        target_id = os.getenv("API_USAGE_REPORT_THREAD_ID") or os.getenv("API_USAGE_REPORT_CHANNEL_ID")
        if target_id is None:
            message = "API_USAGE_REPORT_THREAD_ID or API_USAGE_REPORT_CHANNEL_ID is required"
            raise RuntimeError(message)
        self.target_id = int(target_id)
        self.client = OpenAIOrganizationUsageClient(
            os.environ["OPENAI_ADMIN_API_KEY"], project_id=os.environ["OPENAI_USAGE_PROJECT_ID"]
        )
        self._report_lock = asyncio.Lock()
        self._last_daily_run: datetime.date | None = None
        self._last_openai_retry_at: datetime.datetime | None = None

    async def cog_unload(self) -> None:
        """Cog終了時に日次確認ループを停止する。"""
        self.report_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """DB初期化完了後に計測開始日時を確定して日次ループを開始する。"""
        await self.usage_repository.initialize_measurement()
        if not self.report_loop.is_running():
            self.report_loop.start()

    @tasks.loop(minutes=REPORT_CHECK_INTERVAL_MINUTES)
    async def report_loop(self) -> None:
        """設定時刻後の欠損補完、前日投稿、前々日再集計を行う。"""
        try:
            now = datetime.datetime.now(JST)
            configuration = await self.db.get_configuration()
            if not should_run_daily_report(now, configuration.report_hour, configuration.report_minute):
                return
            async with self._report_lock:
                if self._last_daily_run != now.date():
                    await self._run_daily_reports(now.astimezone(UTC).date())
                    self._last_daily_run = now.date()
                await self._retry_unavailable_openai_cost(now)
        except Exception:
            logger.exception("Failed to process daily Chatbot API usage reports")

    @report_loop.before_loop
    async def before_report_loop(self) -> None:
        """Discord接続とDBテーブル作成が完了するまで待機する。"""
        await self.bot.wait_until_ready()

    @api_usage.command(name="report", description="指定日のAPI利用レポートを投稿または更新します。")
    @app_commands.describe(date="対象期間の開始日 (JST 09:00開始、YYYY-MM-DD)")
    async def report(self, interaction: Interaction, date: str | None = None) -> None:
        """管理者操作で指定日のレポートを同じメッセージへ投稿・更新する。"""
        self._require_admin(interaction)
        report_date = self._parse_report_date(date)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._report_lock:
            await self._post_or_update_report(report_date)
        await interaction.followup.send(
            f"{report_date:%Y-%m-%d} 09:00 ~ "
            f"{report_date + datetime.timedelta(days=1):%Y-%m-%d} 09:00 JST のレポートを投稿または更新しました。",
            ephemeral=True,
        )

    @api_usage.command(name="schedule", description="API日次レポートの投稿時刻を変更します。")
    @app_commands.describe(hour="JSTの時 (0-23)", minute="分 (0-59)")
    async def schedule(self, interaction: Interaction, hour: int, minute: int) -> None:
        """管理者が毎日の投稿時刻をJSTで変更する。"""
        self._require_admin(interaction)
        if not 0 <= hour <= MAXIMUM_HOUR or not 0 <= minute <= MAXIMUM_MINUTE:
            message = "時は0-23、分は0-59で指定してください。"
            raise ArgumentError(message)
        await self.db.set_report_time(hour, minute)
        await interaction.response.send_message(f"投稿時刻を毎日 {hour:02d}:{minute:02d} JST に変更しました。", ephemeral=True)

    @api_usage.command(name="status", description="API日次レポートの現在の設定と状態を表示します。")
    async def status(self, interaction: Interaction) -> None:
        """現在時刻、投稿先、最終成功、次の対象日を管理者だけに表示する。"""
        self._require_admin(interaction)
        now = datetime.datetime.now(JST)
        configuration = await self.db.get_configuration()
        last_delivery = await self.db.get_last_delivery(self.target_id)
        next_date = now.astimezone(UTC).date() - datetime.timedelta(days=1)
        next_end_date = next_date + datetime.timedelta(days=1)
        last_text = (
            f"{last_delivery.report_date:%Y-%m-%d} ({last_delivery.last_updated_at.astimezone(JST):%Y-%m-%d %H:%M JST})"
            if last_delivery is not None
            else "なし"
        )
        await interaction.response.send_message(
            f"現在: {now:%Y-%m-%d %H:%M JST}\n"
            f"投稿時刻: {configuration.report_hour:02d}:{configuration.report_minute:02d} JST\n"
            f"投稿先: <#{self.target_id}>\n最終成功: {last_text}\n"
            f"次の対象期間: {next_date:%Y-%m-%d} 09:00 ~ {next_end_date:%Y-%m-%d} 09:00 JST",
            ephemeral=True,
        )

    async def _run_daily_reports(self, today: datetime.date) -> None:
        """最大7日内の欠損を古い順に補い、前々日を遅延請求反映のため更新する。"""
        started_at = await self.usage_repository.get_measurement_started_at()
        if started_at is None:
            return
        first_date = max(started_at.astimezone(UTC).date(), today - datetime.timedelta(days=MAXIMUM_BACKFILL_DAYS))
        yesterday = today - datetime.timedelta(days=1)
        yesterday_was_delivered = await self.db.has_delivery(yesterday, self.target_id)
        for offset in range((yesterday - first_date).days + 1):
            report_date = first_date + datetime.timedelta(days=offset)
            if not await self.db.has_delivery(report_date, self.target_id):
                await self._post_or_update_report(report_date)
        if yesterday >= first_date and yesterday_was_delivered:
            await self._post_or_update_report(yesterday)
        refresh_date = today - datetime.timedelta(days=2)
        if refresh_date >= first_date and await self.db.has_delivery(refresh_date, self.target_id):
            await self._post_or_update_report(refresh_date)

    async def _retry_unavailable_openai_cost(self, now: datetime.datetime) -> None:
        """前日レポートの確定額取得失敗を1時間に一度だけ再試行する。"""
        if self._last_openai_retry_at is not None and now - self._last_openai_retry_at < OPENAI_RETRY_INTERVAL:
            return
        report_date = now.astimezone(UTC).date() - datetime.timedelta(days=1)
        delivery = await self.db.get_delivery(report_date, self.target_id)
        if (
            delivery is not None
            and not delivery.openai_cost_available
            and now - delivery.last_updated_at.astimezone(JST) >= OPENAI_RETRY_INTERVAL
        ):
            self._last_openai_retry_at = now
            await self._post_or_update_report(report_date)

    async def _post_or_update_report(self, report_date: datetime.date) -> None:
        """指定日の集計を作り、既存投稿の編集または新規投稿を行う。"""
        target = await self._get_target()
        rows = await self.usage_repository.list_for_date(report_date)
        feature_usages = aggregate_feature_usages(rows)
        summary = await self._fetch_openai_summary(report_date)
        started_at = await self.usage_repository.get_measurement_started_at()
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
        message, posted_at = await self._upsert_discord_message(target, report_date, embed)
        await self.db.save_delivery(
            report_date,
            self.target_id,
            message.id,
            posted_at=posted_at,
            openai_cost_available=summary is not None,
        )

    async def _fetch_openai_summary(self, report_date: datetime.date) -> OpenAIUsageSummary | None:
        """OpenAI側の障害時もローカル機能別レポートを継続できるよう失敗を分離する。"""
        try:
            return await self.client.fetch_daily_summary(report_date)
        except Exception:
            logger.exception("Failed to fetch OpenAI organization cost (report_date=%s)", report_date)
            return None

    async def _upsert_discord_message(
        self,
        target: TextChannel | Thread,
        report_date: datetime.date,
        embed: discord.Embed,
    ) -> tuple[discord.Message, datetime.datetime]:
        """配送記録またはフッターから既存投稿を見つけ、なければ新規投稿する。"""
        delivery = await self.db.get_delivery(report_date, self.target_id)
        message = await self._fetch_delivery_message(target, delivery)
        if message is None:
            message = await self._find_report_message(target, report_date)
        now = datetime.datetime.now(JST)
        if message is None:
            return await target.send(embed=embed), now
        await message.edit(embed=embed)
        return message, now

    async def _fetch_delivery_message(
        self, target: TextChannel | Thread, delivery: ReportDelivery | None
    ) -> discord.Message | None:
        """永続化済みIDのメッセージを取得し、削除済みなら未発見として扱う。"""
        if delivery is None:
            return None
        try:
            return await target.fetch_message(delivery.message_id)
        except discord.NotFound:
            return None

    async def _find_report_message(self, target: TextChannel | Thread, report_date: datetime.date) -> discord.Message | None:
        """送信後DB保存前の停止に備え、直近履歴のフッター識別子から投稿を再発見する。"""
        marker = f"{REPORT_MARKER_PREFIX}{report_date.isoformat()}"
        async for message in target.history(limit=100):
            if self.bot.user is None or message.author.id != self.bot.user.id:
                continue
            if any(embed.footer.text and marker in embed.footer.text for embed in message.embeds):
                return message
        return None

    async def _get_target(self) -> TextChannel | Thread:
        """設定されたDiscord投稿先をテキストチャンネルまたはスレッドとして取得する。"""
        target = self.bot.get_channel(self.target_id) or await self.bot.fetch_channel(self.target_id)
        if not isinstance(target, (TextChannel, Thread)):
            message = f"API usage report target must be a text channel or thread: target_id={self.target_id}"
            raise TypeError(message)
        return target

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
        if isinstance(interaction.user, Member) and any(role.id == self.admin_role_id for role in interaction.user.roles):
            return
        message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(message)


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """API利用レポートCogをBotへ登録する。"""
    await bot.add_cog(ApiUsageReporter(bot, session_factory))
    logger.debug("%s is added to the bot.", __name__)
