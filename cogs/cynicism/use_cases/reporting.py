"""冷笑王ランキングの集計、発表、運用設定。"""

import asyncio
import datetime
import os
from decimal import Decimal
from logging import getLogger

import discord
from discord import Interaction, Member
from discord.ext import commands

from cogs.cynicism.constants import JST, MAXIMUM_WEIGHT, MINIMUM_WEIGHT
from cogs.cynicism.models import CynicismRanking, CynicismSettings
from cogs.cynicism.periods import (
    CynicismPeriod,
    CynicismPeriodType,
    current_period,
    format_period,
    latest_completed_period,
    period_from_start_date,
)
from cogs.cynicism.repositories.configuration import CynicismConfigurationRepository
from cogs.cynicism.repositories.reaction import CynicismReactionRepository
from cogs.cynicism.repositories.report import CynicismReportRepository
from cogs.cynicism.services.member_resolution import resolve_identities
from cogs.cynicism.services.message_delivery import CynicismReportMessageDelivery, ReportMessageOwnershipError
from cogs.cynicism.services.ranking import build_ranking
from cogs.cynicism.services.report_builder import (
    build_empty_notice,
    build_ranking_embed,
    format_weight,
    ranking_digest,
)
from cogs.cynicism.services.schedule import publishable_periods, refreshable_periods
from cogs.cynicism.services.scope import channel_scope
from core.exception import ArgumentError, MissingRequiredRoleError
from core.runtime_environment import RuntimeEnvironment

logger = getLogger(__name__)


class CynicismReportUseCases:
    """ランキングの集計と、定期発表・管理コマンドを調整する。"""

    def __init__(
        self,
        bot: commands.Bot,
        runtime_environment: RuntimeEnvironment,
        reaction_repository: CynicismReactionRepository,
        configuration_repository: CynicismConfigurationRepository,
        report_repository: CynicismReportRepository,
    ) -> None:
        self._bot = bot
        self._runtime_environment = runtime_environment
        self._reactions = reaction_repository
        self._configuration = configuration_repository
        self._reports = report_repository
        self._target_id = runtime_environment.cynicism_report_target_id
        self._delivery = CynicismReportMessageDelivery(bot, report_repository, self._target_id)
        self._admin_role_id = int(os.environ["ROLE_ID_BOT_ADMIN"])
        self._server_id = int(os.environ["SERVER_ID"])
        self._report_lock = asyncio.Lock()

    async def process_scheduled_reports(self, now: datetime.datetime | None = None) -> None:
        """発表時刻を過ぎた期間を投稿し、直近の期間を再集計する。"""
        if self._runtime_environment.is_debug:
            return
        settings = await self._configuration.get()
        if settings.is_paused:
            return
        current_time = now or datetime.datetime.now(JST)
        async with self._report_lock:
            earliest_recorded = await self._reactions.earliest_recorded_date()
            for period in publishable_periods(current_time, earliest_recorded):
                if not await self._reports.has_delivery(period, self._target_id):
                    await self._post_or_update(period, settings)
            for period in refreshable_periods(current_time):
                # 遅れて付いた🥶や重み変更を反映するため、発表済みの内容だけを更新する。
                if await self._reports.has_delivery(period, self._target_id):
                    await self._post_or_update(period, settings)

    async def ranking(self, interaction: Interaction, period_type: str, start: str | None) -> None:
        """指定期間のランキングをその場で集計して表示する。"""
        # 閲覧では今の順位を知りたいはずなので、既定を進行中の期間にする。
        period = self._resolve_period(period_type, start, default_to_completed=False)
        await interaction.response.defer(thinking=True)
        settings = await self._configuration.get()
        ranking = await self.build_ranking_for(period, settings, guild=interaction.guild)
        if ranking.is_empty:
            await interaction.followup.send(build_empty_notice(period))
            return
        embed = build_ranking_embed(ranking, updated_at=datetime.datetime.now(JST))
        message = await interaction.followup.send(embed=embed, wait=True)
        # 表示のたびにChatbotが読み込んでトークンを消費しないよう、長期記憶の根拠から外す。
        self._bot.dispatch("exclude_from_long_term_memory", message)

    async def publish(self, interaction: Interaction, period_type: str, start: str | None) -> None:
        """管理者操作で指定期間のランキングを発表チャンネルへ投稿・更新する。"""
        self._require_admin(interaction)
        # 発表は確定した順位を出すものなので、既定を直近の完了済み期間にする。
        period = self._resolve_period(period_type, start, default_to_completed=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self._configuration.get()
        try:
            async with self._report_lock:
                posted = await self._post_or_update(period, settings)
        except ReportMessageOwnershipError:
            logger.exception("Refused to overwrite another Bot's cynicism report")
            await interaction.followup.send(
                "投稿先または配送記録が別のBotの投稿を指しています。本番とデバッグで別の投稿先を設定してください。",
                ephemeral=True,
            )
            return
        if posted is None:
            await interaction.followup.send(build_empty_notice(period), ephemeral=True)
            return
        await interaction.followup.send(
            f"{period.label}冷笑王 ({format_period(period)}) を投稿または更新しました。\n{posted.jump_url}",
            ephemeral=True,
        )

    async def status(self, interaction: Interaction) -> None:
        """重み、一時停止状態、投稿先、最終発表を表示する。"""
        now = datetime.datetime.now(JST)
        settings = await self._configuration.get()
        last_delivery = await self._reports.get_last_delivery(self._target_id)
        last_text = (
            f"{last_delivery.period_type} {last_delivery.period_start:%Y-%m-%d} "
            f"({last_delivery.last_updated_at.astimezone(JST):%Y-%m-%d %H:%M JST})"
            if last_delivery is not None
            else "なし"
        )
        paused_text = (
            f"一時停止中 ({settings.paused_at.astimezone(JST):%Y-%m-%d %H:%M JST} から)"
            if settings.is_paused and settings.paused_at is not None
            else ("一時停止中" if settings.is_paused else "稼働中")
        )
        environment_note = "debug (自動発表停止、手動投稿のみ)" if self._runtime_environment.is_debug else "production"
        await interaction.response.send_message(
            f"現在: {now:%Y-%m-%d %H:%M JST}\n"
            f"集計状態: {paused_text}\n"
            f"重み: Papyrus {format_weight(settings.weights.papyrus)} / "
            f"人間 {format_weight(settings.weights.human)}\n"
            f"投稿先: <#{self._target_id}>\n"
            f"最終発表: {last_text}\n"
            f"実行環境: {environment_note}",
            ephemeral=True,
        )

    async def set_weights(self, interaction: Interaction, papyrus: float | None, human: float | None) -> None:
        """管理者が🥶の重みを変更する。省略した側は据え置く。"""
        self._require_admin(interaction)
        if papyrus is None and human is None:
            message = "papyrus と human の少なくとも一方を指定してください。"
            raise ArgumentError(message)
        updated = await self._configuration.set_weights(
            papyrus=self._validate_weight(papyrus),
            human=self._validate_weight(human),
        )
        await interaction.response.send_message(
            f"重みを Papyrus {format_weight(updated.weights.papyrus)} / "
            f"人間 {format_weight(updated.weights.human)} に変更しました。\n"
            "※過去の期間の集計値も新しい重みで再計算されます。",
            ephemeral=True,
        )

    async def pause(self, interaction: Interaction) -> None:
        """管理者が🥶の記録と自動発表を停止する。"""
        self._require_admin(interaction)
        await self._configuration.set_paused(paused=True, now=datetime.datetime.now(JST))
        await interaction.response.send_message(
            "冷笑ポイントの記録と自動発表を停止しました。`/cynicism ranking` での閲覧は引き続き利用できます。",
            ephemeral=True,
        )

    async def resume(self, interaction: Interaction) -> None:
        """管理者が🥶の記録と自動発表を再開する。"""
        self._require_admin(interaction)
        await self._configuration.set_paused(paused=False, now=datetime.datetime.now(JST))
        await interaction.response.send_message(
            "冷笑ポイントの記録と自動発表を再開しました。停止中に付いた🥶は記録されていません。",
            ephemeral=True,
        )

    async def build_ranking_for(
        self,
        period: CynicismPeriod,
        settings: CynicismSettings,
        *,
        guild: discord.Guild | None = None,
    ) -> CynicismRanking:
        """期間内の🥶と発言数から、2部門のランキングを組み立てる。"""
        scope = channel_scope(self._runtime_environment)
        papyrus_user_id = self._bot.user.id if self._bot.user is not None else 0
        counts = await self._reactions.aggregate_counts(
            period,
            papyrus_user_id=papyrus_user_id,
            scope=scope,
        )
        if not counts:
            return build_ranking(period, [], {}, {}, settings.weights)
        message_counts = await self._reactions.aggregate_message_counts(period, scope=scope)
        member_ids = [entry.member_id for entry in counts]
        stored_names = await self._reactions.get_display_names(member_ids)
        identities = resolve_identities(
            self._bot,
            guild or self._bot.get_guild(self._server_id),
            member_ids,
            stored_names,
        )
        return build_ranking(period, counts, message_counts, identities, settings.weights)

    async def _post_or_update(self, period: CynicismPeriod, settings: CynicismSettings) -> discord.Message | None:
        """集計結果を作り、既存投稿の編集または新規投稿を行う。"""
        ranking = await self.build_ranking_for(period, settings)
        if ranking.is_empty:
            # 対象が無い期間まで毎回投稿すると通知が増えるだけなので、配送記録も残さない。
            return None
        digest = ranking_digest(ranking)
        embed = build_ranking_embed(ranking, updated_at=datetime.datetime.now(JST))
        result = await self._delivery.upsert(period, embed, digest)
        if result.changed:
            await self._reports.save_delivery(
                period,
                self._target_id,
                result.message.id,
                content_digest=digest,
                posted_at=result.updated_at,
            )
        return result.message

    def _resolve_period(self, period_type: str, start: str | None, *, default_to_completed: bool) -> CynicismPeriod:
        """コマンド引数を集計期間へ変換する。開始日は期間内の任意の日でよい。"""
        try:
            resolved_type = CynicismPeriodType(period_type)
        except ValueError as error:
            message = "期間には weekly、monthly、yearly のいずれかを指定してください。"
            raise ArgumentError(message) from error
        if start is None:
            now = datetime.datetime.now(JST)
            return latest_completed_period(resolved_type, now) if default_to_completed else current_period(resolved_type, now)
        try:
            start_date = datetime.date.fromisoformat(start)
        except ValueError as error:
            message = "開始日は YYYY-MM-DD 形式で指定してください。"
            raise ArgumentError(message) from error
        return period_from_start_date(resolved_type, start_date)

    def _validate_weight(self, weight: float | None) -> Decimal | None:
        """重みが許容範囲に収まっているかを検証する。"""
        if weight is None:
            return None
        value = Decimal(str(weight))
        if not MINIMUM_WEIGHT <= value <= MAXIMUM_WEIGHT:
            message = f"重みは {MINIMUM_WEIGHT} 以上 {MAXIMUM_WEIGHT} 以下で指定してください。"
            raise ArgumentError(message)
        return value

    def _require_admin(self, interaction: Interaction) -> None:
        """Bot管理者ロールを持つ利用者だけに操作を許可する。"""
        if isinstance(interaction.user, Member) and any(role.id == self._admin_role_id for role in interaction.user.roles):
            return
        message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(message)
