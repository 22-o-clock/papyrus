import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.api_usage.models import ReportMeasurementState
from cogs.api_usage.openai_usage import (
    JST,
    UTC,
    ModelUsage,
    OpenAIUsageSummary,
    _aggregate_costs,
    _aggregate_model_usage,
    utc_report_period,
)
from cogs.api_usage.services.message_delivery import ApiUsageReportMessageDelivery, ReportMessageOwnershipError
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
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "web_search_calls": 0,
        "code_interpreter_sessions": 0,
        "long_context_input_tokens": 0,
        "long_context_cached_input_tokens": 0,
        "long_context_cache_write_input_tokens": 0,
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

    def test_cache_write_uses_gpt_5_6_write_price(self) -> None:
        usage = aggregate_feature_usages(
            [
                create_usage(
                    "draft_generation",
                    "gpt-5.6-terra",
                    input_tokens=1_000_000,
                    cache_write_input_tokens=200_000,
                )
            ]
        )[0]

        ensure_equal(usage.model_cost, Decimal("2.625"))
        ensure_equal(usage.cache_write_input_tokens, 200_000)

    def test_custom_profile_models_use_configured_token_prices(self) -> None:
        usages = aggregate_feature_usages(
            [
                create_usage("draft_generation", "gpt-5.6", input_tokens=1_000_000, output_tokens=100_000),
                create_usage("draft_generation", "gpt-5.6-luna", input_tokens=1_000_000, output_tokens=100_000),
            ]
        )

        ensure_equal(usages[0].model_cost, Decimal("9.6"))
        ensure_equal(usages[0].unknown_price_models, set())

    def test_nano_judgment_uses_configured_token_prices(self) -> None:
        row = create_usage(
            "response_judgment",
            "gpt-5.4-nano",
            input_tokens=2_000_000,
            cached_input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        row.usage_date = datetime.date(2026, 7, 15)

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

    def test_aggregate_openai_usage_combines_models(self) -> None:
        results = [
            {
                "model": "gpt-5.6-terra",
                "num_model_requests": 2,
                "input_tokens": 100,
                "input_cached_tokens": 25,
                "output_tokens": 10,
            },
            {
                "model": "gpt-5.6-terra",
                "num_model_requests": 1,
                "input_tokens": 50,
                "input_cached_tokens": 0,
                "output_tokens": 5,
            },
        ]

        usages = _aggregate_model_usage(results)

        ensure_equal(len(usages), 1)
        ensure_equal(usages[0].requests, 3)
        ensure_equal(usages[0].input_tokens, 150)
        ensure_equal(usages[0].cached_input_tokens, 25)
        ensure_equal(usages[0].output_tokens, 15)


class ApiUsageEmbedTest(TestCase):
    def test_embed_labels_judgment_and_generation_separately(self) -> None:
        report_date = datetime.date(2026, 7, 15)
        rows = [
            create_usage("response_judgment", "gpt-5.4-nano", input_tokens=1_000),
            create_usage("draft_generation", "gpt-5.6-terra", input_tokens=1_000),
            create_usage(
                "draft_generation_pending_memory_followup",
                "gpt-5.6-luna",
                input_tokens=1_000,
            ),
        ]
        for row in rows:
            row.usage_date = report_date
        features = aggregate_feature_usages(rows)

        embed = build_usage_embed(
            report_date,
            features,
            None,
            ReportMeasurementState(started_at=None, error_count=0),
        )

        field_names = [str(field.name or "") for field in embed.fields]
        ensure(any(name.startswith("応答要否判定 —") for name in field_names))
        ensure(any(name.startswith("応答生成 —") for name in field_names))
        ensure(any(name.startswith("応答生成 (未反映記憶取得後) —") for name in field_names))
        ensure(all("応答生成・行動判断" not in name for name in field_names))
        judgment_field = next(field for field in embed.fields if str(field.name or "").startswith("応答要否判定 —"))
        ensure_contains("判定 1件", str(judgment_field.value))
        followup_field = next(
            field for field in embed.fields if str(field.name or "").startswith("応答生成 (未反映記憶取得後) —")
        )
        ensure_contains("応答 1件", str(followup_field.value))

    def test_embed_prioritizes_feature_costs_and_long_term_memory_breakdown(self) -> None:
        report_date = datetime.date(2026, 7, 14)
        features = aggregate_feature_usages(
            [
                create_usage("draft_generation", "gpt-5.6-terra", input_tokens=1_000, output_tokens=100),
                create_usage("memory_document_update", "gpt-5.6-luna", item_count=2, input_tokens=2_000),
                create_usage("memory_document_shorten", "gpt-5.6-luna", item_count=3, input_tokens=500),
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
        ensure(any("長期記憶文書の更新" in name for name in field_names))
        memory_fields = {
            str(field.name or ""): str(field.value) for field in embed.fields if "長期記憶文書" in str(field.name or "")
        }
        ensure(any("チャンネル 2件" in value for value in memory_fields.values()))
        ensure(any("文書 3件" in value for value in memory_fields.values()))
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

    def test_embed_warns_when_openai_usage_does_not_match_local_measurement(self) -> None:
        report_date = datetime.date(2026, 7, 13)
        features = aggregate_feature_usages(
            [create_usage("draft_generation", "gpt-5.6-terra", input_tokens=100, output_tokens=10)]
        )
        summary = OpenAIUsageSummary(
            report_date=report_date,
            completion_usage=[
                ModelUsage(
                    model="gpt-5.6-terra",
                    requests=2,
                    input_tokens=150,
                    cached_input_tokens=25,
                    output_tokens=15,
                )
            ],
            costs={"Text models": Decimal("0.01")},
            usage_available=True,
        )

        embed = build_usage_embed(
            report_date,
            features,
            summary,
            ReportMeasurementState(started_at=None, error_count=0),
        )

        warning = next(field for field in embed.fields if field.name == "⚠ 確認事項")
        ensure_contains("calls +1 / input +50 / cached +25 / output +5 tokens", str(warning.value))


class ApiUsageObservationTest(IsolatedAsyncioTestCase):
    async def test_records_response_usage_and_tools_without_response_content(self) -> None:
        repository = SimpleNamespace(add=AsyncMock())
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=300_000,
                output_tokens=10_000,
                input_tokens_details=SimpleNamespace(cached_tokens=50_000, cache_write_tokens=25_000),
                output_tokens_details=SimpleNamespace(reasoning_tokens=8_000),
            ),
            output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="code_interpreter_call")],
        )

        async def request() -> object:
            return response

        with (
            patch.object(observability, "_usage_repository", repository),
            self.assertLogs("cogs.chatbot.observability", level="DEBUG") as captured_logs,
        ):
            result = await observability.observe_chatbot_api_call("draft_generation", "gpt-5.6-terra", request())

        ensure(result is response)
        increment = repository.add.await_args.args[0]
        ensure_equal(increment.input_tokens, 300_000)
        ensure_equal(increment.cached_input_tokens, 50_000)
        ensure_equal(increment.cache_write_input_tokens, 25_000)
        ensure_equal(increment.long_context_input_tokens, 300_000)
        ensure_equal(increment.long_context_cache_write_input_tokens, 25_000)
        ensure_equal(increment.web_search_calls, 1)
        ensure_equal(increment.code_interpreter_sessions, 1)
        usage_log = "\n".join(captured_logs.output)
        ensure_contains("input_tokens=300000", usage_log)
        ensure_contains("output_tokens=10000", usage_log)
        ensure_contains("reasoning_tokens=8000", usage_log)
        ensure_contains("total_tokens=310000", usage_log)
        ensure_contains("web_search_calls=1", usage_log)
        ensure_contains("code_interpreter_sessions=1", usage_log)


class ApiUsageDeliveryTest(TestCase):
    def test_rejects_delivery_message_owned_by_another_bot(self) -> None:
        bot = MagicMock()
        bot.user.id = 100
        delivery = ApiUsageReportMessageDelivery(bot, MagicMock(), target_id=200)
        message = MagicMock()
        message.id = 300
        message.author.id = 400

        try:
            delivery.validate_message_owner(message)
        except ReportMessageOwnershipError:
            return
        raise AssertionError

    def test_accepts_delivery_message_owned_by_current_bot(self) -> None:
        bot = MagicMock()
        bot.user.id = 100
        delivery = ApiUsageReportMessageDelivery(bot, MagicMock(), target_id=200)
        message = MagicMock()
        message.id = 300
        message.author.id = 100

        delivery.validate_message_owner(message)
