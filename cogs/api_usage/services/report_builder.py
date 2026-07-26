import datetime
from decimal import Decimal

import discord

from cogs.api_usage.models import FeatureUsage, ReportMeasurementState
from cogs.api_usage.openai_usage import JST, OpenAIUsageSummary
from cogs.api_usage.pricing import PRICING_VERIFIED_ON, estimate_usage_cost
from cogs.chatbot.repositories.api_usage import UTC, ChatbotApiUsageDaily

REPORT_MARKER_PREFIX = "api-usage-report:"
MEMORY_OPERATIONS = {
    "memory_document_update",
    "memory_document_shorten",
    "memory_extraction",
    "memory_reconciliation",
    "memory_embedding",
    "memory_search_embedding",
    "memory_admin_embedding",
}
FEATURE_LABELS = {
    "response_judgment": "応答要否判定",
    "draft_generation": "応答生成",
    "attachment_analysis": "添付ファイル解析",
    "memory_document_update": "長期記憶文書の更新",
    "memory_document_shorten": "長期記憶文書の短縮再生成",
    "memory_extraction": "旧長期記憶の抽出",
    "memory_reconciliation": "旧長期記憶の整合判定",
    "memory_embedding": "旧長期記憶の登録用Embedding",
    "memory_search_embedding": "旧長期記憶の検索用Embedding",
    "memory_admin_embedding": "旧管理更新用Embedding",
}
ITEM_LABELS = {
    "response_judgment": "判定",
    "draft_generation": "応答",
    "attachment_analysis": "添付",
    "memory_document_update": "会話",
    "memory_document_shorten": "会話",
    "memory_extraction": "メッセージ",
    "memory_reconciliation": "判定",
    "memory_embedding": "記憶",
    "memory_search_embedding": "検索クエリ",
    "memory_admin_embedding": "記憶",
}


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
    if openai_summary is not None and openai_summary.costs:
        embed.add_field(
            name="OpenAI請求内訳",
            value="\n".join(
                f"{line_item}: **{_format_usd(cost)}**"
                for line_item, cost in sorted(openai_summary.costs.items(), key=lambda item: item[1], reverse=True)
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

    used_operations = {usage.operation for usage in feature_usages}
    unused = [label for operation, label in FEATURE_LABELS.items() if operation not in used_operations]
    if unused:
        embed.add_field(name="利用なし", value=" / ".join(unused), inline=False)
    warnings = _build_warnings(
        feature_usages,
        openai_summary,
        estimated_total,
        measurement_state.error_count,
        report_is_complete=measurement_state.is_complete,
    )
    if warnings:
        embed.add_field(name="⚠ 確認事項", value="\n".join(warnings), inline=False)
    embed.set_footer(
        text=(
            f"{REPORT_MARKER_PREFIX}{report_date.isoformat()} | cached/cache write input は input token の内数 | "
            f"単価確認 {PRICING_VERIFIED_ON:%Y-%m-%d}"
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
            f"input {usage.input_tokens:,} tokens "
            f"(cached {usage.cached_input_tokens:,} / cache write {usage.cache_write_input_tokens:,}) / "
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
    if not any(differences):
        return None
    requests, input_tokens, cached_input_tokens, output_tokens = differences
    return (
        "OpenAI Usageとの差: "
        f"calls {requests:+,} / input {input_tokens:+,} / "
        f"cached {cached_input_tokens:+,} / output {output_tokens:+,} tokens"
    )


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
