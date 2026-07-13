import datetime
from dataclasses import dataclass
from decimal import Decimal

from cogs.chatbot.repositories.api_usage import ChatbotApiUsageDaily

PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_VERIFIED_ON = datetime.date(2026, 7, 14)
PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """指定日以降に適用する100万token当たりのUSD単価。"""

    effective_from: datetime.date
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    long_input_per_million: Decimal | None = None
    long_cached_input_per_million: Decimal | None = None
    long_output_per_million: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EstimatedUsageCost:
    """1集約行の推定額と内訳。"""

    model_cost: Decimal
    web_search_cost: Decimal
    code_interpreter_cost: Decimal
    price_known: bool

    @property
    def total(self) -> Decimal:
        """モデルとツールの推定額合計を返す。"""
        return self.model_cost + self.web_search_cost + self.code_interpreter_cost


MODEL_PRICES: dict[str, tuple[ModelPrice, ...]] = {
    "gpt-5.6-terra": (
        ModelPrice(
            datetime.date(2026, 7, 13),
            Decimal("2.50"),
            Decimal("0.25"),
            Decimal("15.00"),
            Decimal("5.00"),
            Decimal("0.50"),
            Decimal("22.50"),
        ),
    ),
    "gpt-5.4-mini": (ModelPrice(datetime.date(2026, 7, 13), Decimal("0.75"), Decimal("0.075"), Decimal("4.50")),),
    "text-embedding-3-large": (ModelPrice(datetime.date(2026, 7, 13), Decimal("0.13"), Decimal("0.13"), Decimal()),),
}
WEB_SEARCH_PER_CALL = Decimal("0.01")
CODE_INTERPRETER_PER_SESSION = Decimal("0.03")


def estimate_usage_cost(usage: ChatbotApiUsageDaily) -> EstimatedUsageCost:
    """対象日の有効単価で日次集約行の推定USD額を計算する。"""
    price = _find_model_price(usage.model, usage.usage_date)
    model_cost = Decimal()
    if price is not None:
        cached_tokens = min(usage.input_tokens, usage.cached_input_tokens)
        uncached_tokens = max(0, usage.input_tokens - cached_tokens)
        long_cached_tokens = min(usage.long_context_input_tokens, usage.long_context_cached_input_tokens)
        long_uncached_tokens = max(0, usage.long_context_input_tokens - long_cached_tokens)
        short_cached_tokens = cached_tokens - long_cached_tokens
        short_uncached_tokens = uncached_tokens - long_uncached_tokens
        short_output_tokens = usage.output_tokens - usage.long_context_output_tokens
        model_cost = (
            Decimal(short_uncached_tokens) * price.input_per_million
            + Decimal(short_cached_tokens) * price.cached_input_per_million
            + Decimal(short_output_tokens) * price.output_per_million
        ) / PER_MILLION
        if price.long_input_per_million is not None:
            model_cost += (
                Decimal(long_uncached_tokens) * price.long_input_per_million
                + Decimal(long_cached_tokens) * (price.long_cached_input_per_million or Decimal())
                + Decimal(usage.long_context_output_tokens) * (price.long_output_per_million or Decimal())
            ) / PER_MILLION
    return EstimatedUsageCost(
        model_cost=model_cost,
        web_search_cost=Decimal(usage.web_search_calls) * WEB_SEARCH_PER_CALL,
        code_interpreter_cost=Decimal(usage.code_interpreter_sessions) * CODE_INTERPRETER_PER_SESSION,
        price_known=price is not None,
    )


def _find_model_price(model: str, usage_date: datetime.date) -> ModelPrice | None:
    """指定日に有効な最新のモデル単価を返す。"""
    candidates = [price for price in MODEL_PRICES.get(model, ()) if price.effective_from <= usage_date]
    return max(candidates, key=lambda price: price.effective_from) if candidates else None
