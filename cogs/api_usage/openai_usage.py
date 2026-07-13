import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp

OPENAI_API_BASE_URL = "https://api.openai.com/v1"
JST = datetime.timezone(datetime.timedelta(hours=9))
UTC = datetime.UTC
HTTP_OK = 200


class OpenAIUsageApiError(RuntimeError):
    """OpenAI Organization Usage APIから正常な応答を取得できなかったことを表します。"""


@dataclass
class ModelUsage:
    """モデル別のリクエスト数とトークン利用量。"""

    model: str
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class OpenAIUsageSummary:
    """Discordへ表示する1日分のOpenAI利用量と確定コスト。"""

    report_date: datetime.date
    completion_usage: list[ModelUsage] = field(default_factory=list)
    embedding_usage: list[ModelUsage] = field(default_factory=list)
    costs: dict[str, Decimal] = field(default_factory=dict)
    currency: str = "usd"

    @property
    def total_cost(self) -> Decimal:
        """費目別コストの合計を返します。"""
        return sum(self.costs.values(), start=Decimal())


class OpenAIOrganizationUsageClient:
    """Organization Usage APIとCosts APIから日次集計を取得します。"""

    def __init__(self, admin_api_key: str, *, project_id: str | None = None) -> None:
        self._admin_api_key = admin_api_key
        self._project_id = project_id

    async def fetch_daily_summary(self, report_date: datetime.date) -> OpenAIUsageSummary:
        """指定したUTC暦日の利用量と請求コストを取得します。"""
        start, end = utc_report_period(report_date)
        base_parameters = [
            ("start_time", str(int(start.timestamp()))),
            ("end_time", str(int(end.timestamp()))),
            ("bucket_width", "1d"),
            ("limit", "1"),
        ]
        if self._project_id is not None:
            base_parameters.append(("project_ids", self._project_id))

        headers = {"Authorization": f"Bearer {self._admin_api_key}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            cost_results = await self._fetch_all_results(
                session,
                "/organization/costs",
                [*base_parameters, ("group_by", "line_item")],
            )

        costs, currency = _aggregate_costs(cost_results)
        return OpenAIUsageSummary(
            report_date=report_date,
            costs=costs,
            currency=currency,
        )

    async def _fetch_all_results(
        self,
        session: aiohttp.ClientSession,
        path: str,
        parameters: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """カーソルを辿り、全バケット内の結果を平坦化して返します。"""
        results: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            request_parameters = [*parameters]
            if page is not None:
                request_parameters.append(("page", page))
            async with session.get(f"{OPENAI_API_BASE_URL}{path}", params=request_parameters) as response:
                if response.status != HTTP_OK:
                    body = await response.text()
                    message = f"OpenAI Usage API returned HTTP {response.status}: {body[:500]}"
                    raise OpenAIUsageApiError(message)
                payload = await response.json()

            for bucket in payload.get("data", []):
                results.extend(bucket.get("results", []))
            if not payload.get("has_more"):
                return results
            page = payload.get("next_page")
            if not isinstance(page, str) or not page:
                message = "OpenAI Usage API indicated another page without a cursor"
                raise OpenAIUsageApiError(message)


def _aggregate_model_usage(results: list[dict[str, Any]]) -> list[ModelUsage]:
    """API結果をモデル単位に集約します。"""
    usages: dict[str, ModelUsage] = {}
    for result in results:
        model = str(result.get("model") or "不明なモデル")
        usage = usages.setdefault(model, ModelUsage(model=model))
        usage.requests += int(result.get("num_model_requests") or 0)
        usage.input_tokens += int(result.get("input_tokens") or 0)
        usage.cached_input_tokens += int(result.get("input_cached_tokens") or 0)
        usage.output_tokens += int(result.get("output_tokens") or 0)
    return sorted(usages.values(), key=lambda usage: usage.requests, reverse=True)


def utc_report_period(report_date: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """Costs APIと一致するUTC日次期間の開始・終了を返す。"""
    start = datetime.datetime.combine(report_date, datetime.time.min, tzinfo=UTC)
    return start, start + datetime.timedelta(days=1)


def _aggregate_costs(results: list[dict[str, Any]]) -> tuple[dict[str, Decimal], str]:
    """API結果を費目単位に集約し、通貨コードとともに返します。"""
    costs: defaultdict[str, Decimal] = defaultdict(Decimal)
    currency = "usd"
    for result in results:
        line_item = str(result.get("line_item") or "その他")
        amount = result.get("amount") or {}
        costs[line_item] += Decimal(str(amount.get("value") or 0))
        currency = str(amount.get("currency") or currency)
    return dict(costs), currency
