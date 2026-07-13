from __future__ import annotations

import datetime
from collections import defaultdict
from logging import getLogger
from typing import TYPE_CHECKING, Any

from .repositories.api_usage import UTC, ApiUsageIncrement, ChatbotApiUsageRepository

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = getLogger(__name__)
_usage_repository: ChatbotApiUsageRepository | None = None
_measurement_errors: defaultdict[datetime.date, int] = defaultdict(int)
LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 272_000


def configure_chatbot_api_usage(session_factory: async_sessionmaker[AsyncSession]) -> ChatbotApiUsageRepository:
    """Chatbot API計測の保存先を設定する。"""
    global _usage_repository  # noqa: PLW0603 - Bot起動時に一度だけ共有保存先を注入するため。
    _usage_repository = ChatbotApiUsageRepository(session_factory)
    return _usage_repository


def get_measurement_error_count(usage_date: datetime.date) -> int:
    """DB保存に失敗した呼び出し数をプロセス内で返す。"""
    return _measurement_errors[usage_date]


def log_chatbot_api_call(
    operation: str,
    model: str,
    *,
    item_count: int = 1,
    custom_profile: str | None = None,
) -> None:
    """長期テストでAPI呼び出し回数を集計できる構造化ログを残します。"""
    logger.info(
        "Chatbot API call (operation=%s, model=%s, item_count=%s, custom_profile=%s)",
        operation,
        model,
        item_count,
        custom_profile,
    )


async def observe_chatbot_api_call[T](
    operation: str,
    model: str,
    request: Awaitable[T],
    *,
    item_count: int = 1,
    custom_profile: str | None = None,
) -> T:
    """OpenAI呼び出しを実行し、結果のusageだけを失敗非伝播で日次集約へ記録する。"""
    log_chatbot_api_call(operation, model, item_count=item_count, custom_profile=custom_profile)
    try:
        response = await request
    except Exception:
        await _record_increment(ApiUsageIncrement(operation=operation, model=model, succeeded=False, item_count=item_count))
        raise

    usage = getattr(response, "usage", None)
    input_tokens = _integer_attribute(usage, "input_tokens", "prompt_tokens")
    output_tokens = _integer_attribute(usage, "output_tokens", "completion_tokens")
    cached_input_tokens = _nested_integer_attribute(usage, "input_tokens_details", "cached_tokens")
    web_search_calls, code_interpreter_sessions = _count_response_tools(response)
    is_long_context = model.startswith("gpt-5.6-") and input_tokens > LONG_CONTEXT_INPUT_TOKEN_THRESHOLD
    await _record_increment(
        ApiUsageIncrement(
            operation=operation,
            model=model,
            succeeded=True,
            item_count=item_count,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            long_context_input_tokens=input_tokens if is_long_context else 0,
            long_context_cached_input_tokens=cached_input_tokens if is_long_context else 0,
            long_context_output_tokens=output_tokens if is_long_context else 0,
            web_search_calls=web_search_calls,
            code_interpreter_sessions=code_interpreter_sessions,
        )
    )
    return response


async def _record_increment(increment: ApiUsageIncrement) -> None:
    """計測保存の障害を利用者向けChatbot処理から切り離す。"""
    if _usage_repository is None:
        logger.warning("Chatbot API usage repository is not configured")
        return
    try:
        await _usage_repository.add(increment)
    except Exception:
        usage_date = datetime.datetime.now(UTC).date()
        _measurement_errors[usage_date] += 1
        logger.exception("Failed to persist Chatbot API usage (operation=%s, model=%s)", increment.operation, increment.model)


def _integer_attribute(value: object, *names: str) -> int:
    """SDK版ごとの差を許容しながら最初に見つかった整数属性を返す。"""
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return int(candidate)
    return 0


def _nested_integer_attribute(value: object, parent_name: str, child_name: str) -> int:
    """usage内の詳細オブジェクトから整数値を安全に取得する。"""
    parent = getattr(value, parent_name, None)
    return _integer_attribute(parent, child_name)


def _count_response_tools(response: object) -> tuple[int, int]:
    """Responses API出力からWeb検索回数とコンテナセッション有無を数える。"""
    output: list[Any] = getattr(response, "output", []) or []
    types = [getattr(item, "type", None) for item in output]
    web_search_calls = sum(item_type == "web_search_call" for item_type in types)
    code_interpreter_sessions = int("code_interpreter_call" in types)
    return web_search_calls, code_interpreter_sessions
