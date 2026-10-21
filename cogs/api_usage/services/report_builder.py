import datetime
from decimal import Decimal

import discord

from cogs.api_usage.models import FeatureUsage, ReportMeasurementState
from cogs.api_usage.openai_usage import JST, OpenAIUsageSummary
from cogs.api_usage.pricing import PRICING_VERIFIED_ON, estimate_usage_cost
from cogs.chatbot.repositories.api_usage import UTC, ChatbotApiUsageDaily

REPORT_MARKER_PREFIX = "api-usage-report:"


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
        usage.cache_write_input_tokens += row.cache_write_input_tokens
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
    """合計費用と利用量を中心に簡潔な日次Embedを作ります。"""
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
    requests = sum(usage.success_count + usage.failure_count for usage in feature_usages)
    failures = sum(usage.failure_count for usage in feature_usages)
    embed.add_field(
        name="利用量",
        value=(
            f"リクエスト **{requests:,}回** (失敗 {failures:,}回)\n"
            f"入力 **{sum(u.input_tokens for u in feature_usages):,}** / "
            f"出力 **{sum(u.output_tokens for u in feature_usages):,}** トークン"
        ),
        inline=False,
    )
    warnings = _build_warnings(
        feature_usages,
        openai_summary,
        estimated_total,
        measurement_state.error_count,
        report_is_complete=measurement_state.is_complete,
    )
    if warnings:
        embed.add_field(name="⚠ 確認事項", value="\n".join(warnings), inline=False)
    embed.set_footer(text=(f"{REPORT_MARKER_PREFIX}{report_date.isoformat()} | 単価確認 {PRICING_VERIFIED_ON:%Y-%m-%d}"))
    return embed


def _format_cost_summary(
    exact_total: Decimal | None,
    estimated_total: Decimal,
    measurement_started_at: datetime.datetime | None,
    report_date: datetime.date,
    *,
    report_is_complete: bool,
) -> str:
    """確定額・推定額と集計状況を短い概要にする。"""
    exact_text = _format_usd(exact_total) if exact_total is not None else "取得できませんでした (後で再試行)"
    openai_label = "OpenAI確定額" if report_is_complete else "OpenAI集計額 (暫定)"
    lines = [f"{openai_label}: **{exact_text}**", f"推定額: **{_format_usd(estimated_total)}**"]
    if measurement_started_at is not None and measurement_started_at.astimezone(UTC).date() == report_date:
        lines.append(f"※ 計測は {measurement_started_at.astimezone(JST):%Y-%m-%d %H:%M:%S JST} からの部分集計です。")
    if not report_is_complete:
        lines.append("※ 対象期間は進行中です。完了後の自動処理で同じ投稿を更新します。")
    return "\n".join(lines)


def _build_warnings(
    usages: list[FeatureUsage],
    openai_summary: OpenAIUsageSummary | None,
    estimated_total: Decimal,
    measurement_error_count: int,
    *,
    report_is_complete: bool,
) -> list[str]:
    """単価不足・大きな差額・Usage不一致・計測保存失敗だけを警告する。"""
    warnings: list[str] = []
    exact_total = openai_summary.total_cost if openai_summary is not None else None
    unknown_models = sorted({model for usage in usages for model in usage.unknown_price_models})
    if unknown_models:
        warnings.append(f"単価未登録モデル: {', '.join(unknown_models)} (該当model token費は推定額に未反映)")
    if exact_total is not None and exact_total:
        difference = abs(exact_total - estimated_total)
        if difference > Decimal("0.05") and difference / exact_total > Decimal("0.10"):
            warnings.append(f"確定額との差が {_format_usd(difference)} ({difference / exact_total * 100:.1f}%) あります。")
    if openai_summary is not None and openai_summary.usage_available and report_is_complete:
        usage_difference = _format_openai_usage_difference(usages, openai_summary)
        if usage_difference is not None:
            warnings.append(usage_difference)
    if measurement_error_count:
        warnings.append(f"計測DBへの保存失敗を {measurement_error_count:,} calls 検出しました。")
    return warnings


def _format_openai_usage_difference(
    usages: list[FeatureUsage],
    openai_summary: OpenAIUsageSummary,
) -> str | None:
    """OpenAI Usage APIとローカル計測の総量差を警告文へ整形する。"""
    image_requests = sum(usage.success_count for usage in usages if usage.operation == "image_generation")
    usages = [usage for usage in usages if usage.operation != "image_generation"]
    openai_usages = [*openai_summary.completion_usage, *openai_summary.embedding_usage]
    openai_requests = sum(usage.requests for usage in openai_usages)
    openai_input_tokens = sum(usage.input_tokens for usage in openai_usages)
    openai_cached_input_tokens = sum(usage.cached_input_tokens for usage in openai_usages)
    openai_output_tokens = sum(usage.output_tokens for usage in openai_usages)
    local_requests = sum(usage.success_count for usage in usages)
    local_input_tokens = sum(usage.input_tokens for usage in usages)
    local_cached_input_tokens = sum(usage.cached_input_tokens for usage in usages)
    local_output_tokens = sum(usage.output_tokens for usage in usages)
    differences = (
        openai_requests - local_requests,
        openai_input_tokens - local_input_tokens,
        openai_cached_input_tokens - local_cached_input_tokens,
        openai_output_tokens - local_output_tokens,
    )
    image_difference = openai_summary.image_requests - image_requests
    if not any(differences) and not image_difference:
        return None
    requests, input_tokens, cached_input_tokens, output_tokens = differences
    return (
        "OpenAI Usageとの差: "
        f"calls {requests:+,} / input {input_tokens:+,} / "
        f"cached {cached_input_tokens:+,} / output {output_tokens:+,} tokens"
        + (f" / 画像生成 {image_difference:+,} calls" if image_difference else "")
    )


def _format_usd(amount: Decimal | None) -> str:
    """USDを小数3桁で、非ゼロの極小額だけ不等号付きで表示する。"""
    if amount is None:
        return "—"
    if amount != 0 and abs(amount) < Decimal("0.001"):
        return "<$0.001" if amount > 0 else ">-$0.001"
    return f"${amount:.3f}"
