import datetime
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class FeatureUsage:
    """複数モデル行を機能単位にまとめた表示用集約。"""

    operation: str
    success_count: int = 0
    failure_count: int = 0
    item_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
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
