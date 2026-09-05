"""冷笑王ランキングの集計、発表、運用設定。"""

import asyncio
import datetime
import os
from dataclasses import replace
from logging import getLogger

import discord
from discord import Interaction, Member
from discord.ext import commands

from cogs.cynicism.constants import JST
from cogs.cynicism.models import CynicismRanking, TopCynicismMessage
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
from cogs.cynicism.services.message_list import build_message_embeds
from cogs.cynicism.services.ranking import build_ranking
from cogs.cynicism.services.report_builder import (
    build_empty_notice,
    build_ranking_embed,
    build_top_messages_files,
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
        """集計・設定・発表履歴を接続し、投稿の重複実行を防ぐロックを用意する。"""
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
                # 対象が無く投稿しなかった期間も処理済みとして記録するため、毎分の再集計にはならない。
                if not await self._reports.has_delivery(period, self._target_id):
                    await self._post_or_update(period, record_empty=True)
            for period in refreshable_periods(current_time):
                # 遅れて付いた🥶を反映するため、投稿済みの内容だけを更新する。
                delivery = await self._reports.get_delivery(period, self._target_id)
                if delivery is not None and delivery.is_posted:
                    await self._post_or_update(period, record_empty=False)

    async def ranking(self, interaction: Interaction, period_type: str, start: str | None) -> None:
        """指定期間のランキングをその場で集計して表示する。"""
        # 閲覧では今の順位を知りたいはずなので、既定を進行中の期間にする。
        period = self._resolve_period(period_type, start, default_to_completed=False)
        await interaction.response.defer(thinking=True)
        ranking = await self.build_ranking_for(period, guild=interaction.guild)
        if ranking.is_empty:
            await interaction.followup.send(build_empty_notice(period))
            return
        embed = build_ranking_embed(ranking, updated_at=datetime.datetime.now(JST))
        message = await interaction.followup.send(embed=embed, files=build_top_messages_files(ranking), wait=True)
        # 表示のたびにChatbotが読み込んでトークンを消費しないよう、長期記憶の根拠から外す。
        self._bot.dispatch("exclude_from_long_term_memory", message)

    async def show_messages(
        self,
        interaction: Interaction,
        member: discord.Member,
        period_type: str,
        start: str | None,
    ) -> None:
        """指定メンバーの冷笑ポイント対象発言を、実行者だけにEmbedで紹介する。"""
        period = self._resolve_period(period_type, start, default_to_completed=False)
        await interaction.response.defer(ephemeral=True, thinking=True)
        scope = channel_scope(self._runtime_environment)
        papyrus_user_id = self._bot.user.id if self._bot.user is not None else 0
        records = await self._reactions.list_member_reactions(
            period, member_id=member.id, scope=scope, papyrus_user_id=papyrus_user_id
        )
        if not records:
            await interaction.followup.send(
                f"{member.display_name} の{period.label}冷笑ポイント ({format_period(period)}) は見つかりませんでした。",
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            return

        reactor_ids = sorted({reactor_id for record in records for reactor_id in record.reactor_ids})
        stored_names = await self._reactions.get_display_names(reactor_ids)
        identities = resolve_identities(self._bot, interaction.guild, reactor_ids, stored_names)

        embeds = build_message_embeds(
            records,
            period=period,
            display_name=member.display_name,
            identities=identities,
            guild_id=self._server_id,
        )
        for embed in embeds:
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    async def publish(self, interaction: Interaction, period_type: str, start: str | None) -> None:
        """管理者操作で指定期間のランキングを発表チャンネルへ投稿・更新する。"""
        self._require_admin(interaction)
        # 発表は確定した順位を出すものなので、既定を直近の完了済み期間にする。
        period = self._resolve_period(period_type, start, default_to_completed=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self._report_lock:
                # 手動確認で進行中の期間を空と確定させないよう、ここでは処理済みにしない。
                posted = await self._post_or_update(period, record_empty=False)
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
        """一時停止状態、投稿先、最終発表を表示する。"""
        # DBへの問い合わせが3秒の応答期限を超えることがあるため、先に応答を保留する。
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = datetime.datetime.now(JST)
        settings = await self._configuration.get()
        last_delivery = await self._reports.get_last_delivery(self._target_id)
        last_text = (
            f"{last_delivery.period_type} {last_delivery.period_start:%Y-%m-%d} "
            f"({last_delivery.last_processed_at.astimezone(JST):%Y-%m-%d %H:%M JST})"
            if last_delivery is not None
            else "なし"
        )
        paused_text = (
            f"一時停止中 ({settings.paused_at.astimezone(JST):%Y-%m-%d %H:%M JST} から)"
            if settings.is_paused and settings.paused_at is not None
            else ("一時停止中" if settings.is_paused else "稼働中")
        )
        environment_note = "debug (自動発表停止、手動投稿のみ)" if self._runtime_environment.is_debug else "production"
        await interaction.followup.send(
            f"現在: {now:%Y-%m-%d %H:%M JST}\n"
            f"集計状態: {paused_text}\n"
            f"投稿先: <#{self._target_id}>\n"
            f"最終発表: {last_text}\n"
            f"実行環境: {environment_note}",
            ephemeral=True,
        )

    async def pause(self, interaction: Interaction) -> None:
        """管理者が🥶の記録と自動発表を停止する。"""
        await self._set_paused(interaction, paused=True)

    async def resume(self, interaction: Interaction) -> None:
        """管理者が🥶の記録と自動発表を再開する。"""
        await self._set_paused(interaction, paused=False)

    async def _set_paused(self, interaction: Interaction, *, paused: bool) -> None:
        """管理者を確認し、応答を保留してから停止状態を保存・通知する。"""
        self._require_admin(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._configuration.set_paused(paused=paused, now=datetime.datetime.now(JST))
        notice = (
            "冷笑ポイントの記録と自動発表を停止しました。`/cynicism ranking` での閲覧は引き続き利用できます。"
            if paused
            else "冷笑ポイントの記録と自動発表を再開しました。停止中に付いた🥶は記録されていません。"
        )
        await interaction.followup.send(notice, ephemeral=True)

    async def build_ranking_for(
        self,
        period: CynicismPeriod,
        *,
        guild: discord.Guild | None = None,
    ) -> CynicismRanking:
        """同じ期間・チャンネル範囲から順位表と同率1位の発言を組み立てる。

        Args:
            period: 投稿日時を基準にした集計期間。
            guild: 表示名とBot判定に使うサーバー。省略時は設定済みサーバーを使う。

        Returns:
            冷笑率順位、参考の合計順位、最多ポイントの全発言を含む集計結果。
            記録がない場合は空の順位表を返す。

        """
        scope = channel_scope(self._runtime_environment)
        papyrus_user_id = self._bot.user.id if self._bot.user is not None else 0
        counts = await self._reactions.aggregate_counts(
            period,
            papyrus_user_id=papyrus_user_id,
            scope=scope,
        )
        if not counts:
            return build_ranking(period, [], {}, {})
        message_counts = await self._reactions.aggregate_message_counts(period, scope=scope)
        member_ids = [entry.member_id for entry in counts]
        stored_names = await self._reactions.get_display_names(member_ids)
        identities = resolve_identities(
            self._bot,
            guild or self._bot.get_guild(self._server_id),
            member_ids,
            stored_names,
        )
        ranking = build_ranking(period, counts, message_counts, identities)
        top_messages = await self._reactions.most_reacted_messages(
            period,
            member_ids=[entry.member_id for entry in ranking.total_entries],
            scope=scope,
            papyrus_user_id=papyrus_user_id,
        )
        return replace(
            ranking,
            top_messages=tuple(
                TopCynicismMessage(
                    message_id=top.message_id,
                    channel_id=top.channel_id,
                    member_id=top.member_id,
                    display_name=identities[top.member_id].display_name,
                    points=top.reaction_count,
                    guild_id=guild.id if guild is not None else self._server_id,
                )
                for top in top_messages
            ),
        )

    async def _post_or_update(
        self,
        period: CynicismPeriod,
        *,
        record_empty: bool,
    ) -> discord.Message | None:
        """集計結果を作り、既存投稿の編集または新規投稿を行う。"""
        ranking = await self.build_ranking_for(period)
        if ranking.is_empty:
            # 対象が無い期間は投稿しない。定期処理では処理済みとして記録し、再集計を繰り返さない。
            if record_empty:
                await self._reports.save_empty(period, self._target_id, processed_at=datetime.datetime.now(JST))
            return None
        digest = ranking_digest(ranking)
        embed = build_ranking_embed(ranking, updated_at=datetime.datetime.now(JST))
        result = await self._delivery.upsert(period, embed, digest, files=build_top_messages_files(ranking))
        if result.changed:
            await self._reports.save_posted(
                period,
                self._target_id,
                result.message.id,
                content_digest=digest,
                processed_at=result.updated_at,
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

    def _require_admin(self, interaction: Interaction) -> None:
        """Bot管理者ロールを持つ利用者だけに操作を許可する。"""
        if isinstance(interaction.user, Member) and any(role.id == self._admin_role_id for role in interaction.user.roles):
            return
        message = "コマンドを実行するのに必要なロールがありません。"
        raise MissingRequiredRoleError(message)
