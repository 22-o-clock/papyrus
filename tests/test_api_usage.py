import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from cogs.api_usage.models import ReportMeasurementState
from cogs.api_usage.openai_usage import JST, UTC, OpenAIUsageSummary, _aggregate_costs, utc_report_period
from cogs.api_usage.services.report_builder import aggregate_feature_usages, build_usage_embed
from cogs.api_usage.use_cases.reporting import should_run_daily_report, validate_report_date
from cogs.chatbot import observability
from cogs.chatbot.repositories.api_usage import ChatbotApiUsageDaily, utc_usage_date
from core.exception.exception import ArgumentError


def create_usage(
    operation: str,
    model: str,
    **overrides: int,
) -> ChatbotApiUsageDaily:
    """日次レポートの単体テスト用にORM集約行を生成する。"""
    values = {
        "success_count": 1,
        "failure_count": 0,
        "item_count": 1,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "web_search_calls": 0,
        "code_interpreter_sessions": 0,
        "long_context_input_tokens": 0,
        "long_context_cached_input_tokens": 0,
        "long_context_output_tokens": 0,
        **overrides,
    }
    return ChatbotApiUsageDaily(usage_date=datetime.date(2026, 7, 14), operation=operation, model=model, **values)


def ensure(condition: object) -> None:
    """lint設定と両立する形でテスト条件を検証する。"""
    if not condition:
        raise AssertionError


def ensure_equal(actual: object, expected: object) -> None:
    """値が一致しなければテストを失敗させる。"""
    if actual != expected:
        raise AssertionError


def ensure_contains(needle: str, haystack: str) -> None:
    """期待文字列が含まれなければテストを失敗させる。"""
    if needle not in haystack:
        raise AssertionError


class ApiUsageScheduleTest(TestCase):
    def test_runs_at_scheduled_time(self) -> None:
        now = datetime.datetime(2026, 7, 14, 9, 0, tzinfo=JST)

        ensure(should_run_daily_report(now, 9, 0))

    def test_does_not_run_before_scheduled_time(self) -> None:
        now = datetime.datetime(2026, 7, 14, 8, 59, tzinfo=JST)

        ensure(not should_run_daily_report(now, 9, 0))

    def test_usage_and_cost_period_use_same_utc_date(self) -> None:
        recorded_at = datetime.datetime(2026, 7, 14, 2, 0, tzinfo=JST)

        usage_date = utc_usage_date(recorded_at)
        start, end = utc_report_period(usage_date)

        ensure_equal(usage_date, datetime.date(2026, 7, 13))
        ensure_equal(start, datetime.datetime(2026, 7, 13, tzinfo=UTC))
        ensure_equal(end, datetime.datetime(2026, 7, 14, tzinfo=UTC))

    def test_current_utc_date_is_allowed_and_future_is_rejected(self) -> None:
        current_date = datetime.date(2026, 7, 13)

        validate_report_date(current_date, current_date)
        try:
            validate_report_date(current_date + datetime.timedelta(days=1), current_date)
        except ArgumentError:
            return
        raise AssertionError


class ApiUsageAggregationTest(TestCase):
    def test_aggregates_models_by_chatbot_operation(self) -> None:
        rows = [
            create_usage(
                "draft_generation",
                "gpt-5.6-terra",
                success_count=2,
                item_count=2,
                input_tokens=1_000_000,
                cached_input_tokens=200_000,
                output_tokens=100_000,
                web_search_calls=2,
                code_interpreter_sessions=1,
            ),
            create_usage(
                "draft_generation",
                "gpt-5.4-mini",
                failure_count=1,
                item_count=1,
                input_tokens=100_000,
            ),
        ]

        usages = aggregate_feature_usages(rows)

        ensure_equal(len(usages), 1)
        usage = usages[0]
        ensure_equal(usage.success_count, 3)
        ensure_equal(usage.failure_count, 1)
        ensure_equal(usage.input_tokens, 1_100_000)
        ensure_equal(usage.cached_input_tokens, 200_000)
        ensure_equal(usage.web_search_calls, 2)
        ensure_equal(usage.code_interpreter_sessions, 1)
        ensure_equal(usage.estimated_cost, Decimal("3.675"))

    def test_cached_input_is_not_charged_twice(self) -> None:
        rows = [
            create_usage(
                "draft_generation",
                "gpt-5.6-terra",
                input_tokens=1_000_000,
                cached_input_tokens=1_000_000,
            )
        ]

        usage = aggregate_feature_usages(rows)[0]

        ensure_equal(usage.model_cost, Decimal("0.25"))

    def test_nano_judgment_uses_configured_token_prices(self) -> None:
        row = create_usage(
            "response_judgment",
            "gpt-5.4-nano",
            input_tokens=2_000_000,
            cached_input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        row.usage_date = datetime.date(2026, 7, 16)

        usage = aggregate_feature_usages([row])[0]

        ensure_equal(usage.model_cost, Decimal("1.47"))

    def test_unknown_model_is_explicitly_marked(self) -> None:
        usage = aggregate_feature_usages([create_usage("draft_generation", "unknown-model", input_tokens=10_000)])[0]

        ensure_equal(usage.estimated_cost, Decimal())
        ensure_equal(usage.unknown_price_models, {"unknown-model"})

    def test_long_context_tokens_use_long_context_price(self) -> None:
        usage = aggregate_feature_usages(
            [
                create_usage(
                    "draft_generation",
                    "gpt-5.6-terra",
                    input_tokens=300_000,
                    output_tokens=10_000,
                    long_context_input_tokens=300_000,
                    long_context_output_tokens=10_000,
                )
            ]
        )[0]

        ensure_equal(usage.model_cost, Decimal("1.725"))

    def test_aggregate_costs_combines_line_items(self) -> None:
        results = [
            {"line_item": "Text models", "amount": {"value": 0.1, "currency": "usd"}},
            {"line_item": "Text models", "amount": {"value": 0.02, "currency": "usd"}},
        ]

        costs, currency = _aggregate_costs(results)

        ensure_equal(costs, {"Text models": Decimal("0.12")})
        ensure_equal(currency, "usd")


class ApiUsageEmbedTest(TestCase):
    def test_embed_prioritizes_feature_costs_and_long_term_memory_breakdown(self) -> None:
        report_date = datetime.date(2026, 7, 14)
        features = aggregate_feature_usages(
            [
                create_usage("draft_generation", "gpt-5.6-terra", input_tokens=1_000, output_tokens=100),
                create_usage("memory_extraction", "gpt-5.6-terra", item_count=20, input_tokens=2_000),
                create_usage("memory_embedding", "text-embedding-3-large", item_count=3, input_tokens=500),
            ]
        )
        summary = OpenAIUsageSummary(report_date=report_date, costs={"Text models": Decimal("0.1234")})

        embed = build_usage_embed(
            report_date,
            features,
            summary,
            ReportMeasurementState(started_at=datetime.datetime(2026, 7, 14, 12, 30, tzinfo=JST), error_count=0),
        )

        field_names = [str(field.name or "") for field in embed.fields]
        ensure_equal(field_names[0], "コスト概要")
        ensure(any(name.startswith("長期記憶 合計") for name in field_names))
        ensure(any("長期記憶の抽出" in name for name in field_names))
        ensure_contains("input 1,000 tokens", "\n".join(str(field.value) for field in embed.fields))
        ensure_contains("部分集計", str(embed.fields[0].value))
        ensure_contains("api-usage-report:2026-07-14", embed.footer.text or "")
        ensure_contains("対象期間: 2026-07-14 09:00 ~ 2026-07-15 09:00 JST", embed.description or "")
        ensure("UTC" not in (embed.description or ""))

    def test_embed_warns_when_openai_cost_is_unavailable(self) -> None:
        embed = build_usage_embed(
            datetime.date(2026, 7, 14),
            [],
            None,
            ReportMeasurementState(started_at=None, error_count=0),
        )

        ensure_contains("後で再試行", str(embed.fields[0].value))

    def test_in_progress_utc_date_is_marked_provisional(self) -> None:
        embed = build_usage_embed(
            datetime.date(2026, 7, 13),
            [],
            OpenAIUsageSummary(report_date=datetime.date(2026, 7, 13), costs={"Text models": Decimal("5.3")}),
            ReportMeasurementState(started_at=None, error_count=0, is_complete=False),
        )

        summary = str(embed.fields[0].value)
        ensure_contains("OpenAI集計額 (暫定)", summary)
        ensure_contains("完了後の自動処理で同じ投稿を更新", summary)


class ApiUsageObservationTest(IsolatedAsyncioTestCase):
    async def test_records_response_usage_and_tools_without_response_content(self) -> None:
        repository = SimpleNamespace(add=AsyncMock())
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=300_000,
                output_tokens=10_000,
                input_tokens_details=SimpleNamespace(cached_tokens=50_000),
            ),
            output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="code_interpreter_call")],
        )

        async def request() -> object:
            return response

        with patch.object(observability, "_usage_repository", repository):
            result = await observability.observe_chatbot_api_call(
                "draft_generation",
                "gpt-5.6-terra",
                request(),
            )

        ensure(result is response)
        increment = repository.add.await_args.args[0]
        ensure_equal(increment.input_tokens, 300_000)
        ensure_equal(increment.cached_input_tokens, 50_000)
        ensure_equal(increment.long_context_input_tokens, 300_000)
        ensure_equal(increment.web_search_calls, 1)
        ensure_equal(increment.code_interpreter_sessions, 1)
