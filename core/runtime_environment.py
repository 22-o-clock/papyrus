from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from logging import getLogger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = getLogger(__name__)

BOT_ENVIRONMENT_KEY = "BOT_ENVIRONMENT"
DEBUG_CHATBOT_CHANNEL_ID_KEY = "CHANNEL_ID_DEBUG_CHATBOT"
PRODUCTION_API_USAGE_REPORT_TARGET_ID_KEY = "THREAD_ID_API_USAGE_REPORT"
DEBUG_API_USAGE_REPORT_TARGET_ID_KEY = "THREAD_ID_DEBUG_API_USAGE_REPORT"
OPTIONAL_ENVIRONMENT_KEYS = (
    "AUDIT_IMMUNITY",
    "DEBUG",
    "LASTFM_IDS",
    "LOG_LEVEL",
)

REQUIRED_ENVIRONMENT_KEYS = (
    "CHATBOT_SUPABASE_CONNECTION_STRING",
    DEBUG_CHATBOT_CHANNEL_ID_KEY,
    "CHANNEL_ID_LISTEN_ONLY_MEMBER",
    "CHANNEL_ID_LOBBY",
    DEBUG_API_USAGE_REPORT_TARGET_ID_KEY,
    "DISCORD_BOT_TOKEN",
    "LASTFM_API_KEY",
    "OPENAI_ADMIN_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_USAGE_PROJECT_ID",
    "ROLE_ID_BOT_ADMIN",
    "SERVER_ID",
    "SUPABASE_CONNECTION_STRING",
    "THREAD_ID_ANTHYME_LOG",
    "THREAD_ID_LOG",
    PRODUCTION_API_USAGE_REPORT_TARGET_ID_KEY,
    "VOICEVOX_URL",
)
INTEGER_ENVIRONMENT_KEYS = (
    DEBUG_CHATBOT_CHANNEL_ID_KEY,
    "CHANNEL_ID_LISTEN_ONLY_MEMBER",
    "CHANNEL_ID_LOBBY",
    DEBUG_API_USAGE_REPORT_TARGET_ID_KEY,
    "ROLE_ID_BOT_ADMIN",
    "SERVER_ID",
    "THREAD_ID_ANTHYME_LOG",
    "THREAD_ID_LOG",
    PRODUCTION_API_USAGE_REPORT_TARGET_ID_KEY,
)

CONFIGURED_ENVIRONMENT_KEYS = frozenset((*REQUIRED_ENVIRONMENT_KEYS, *OPTIONAL_ENVIRONMENT_KEYS, BOT_ENVIRONMENT_KEY))


class BotEnvironment(StrEnum):
    """Botが共有資源へ持つ権限を表します。"""

    PRODUCTION = "production"
    DEBUG = "debug"


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """起動時に確定した実行環境とChatbotのテスト範囲。"""

    environment: BotEnvironment
    chatbot_test_channel_ids: frozenset[int]
    api_usage_report_target_id: int

    @property
    def is_debug(self) -> bool:
        """デバッグ用Botとして起動している場合にTrueを返します。"""
        return self.environment is BotEnvironment.DEBUG

    @property
    def is_production(self) -> bool:
        """本番Botとして起動している場合にTrueを返します。"""
        return self.environment is BotEnvironment.PRODUCTION

    def should_process_chatbot_channel(self, channel_id: int) -> bool:
        """環境ごとの完全一致リストに従いChatbot処理可否を返します。"""
        is_test_channel = channel_id in self.chatbot_test_channel_ids
        return is_test_channel if self.is_debug else not is_test_channel


_runtime_environment: RuntimeEnvironment | None = None


def configure_runtime_environment(environ: Mapping[str, str] | None = None) -> RuntimeEnvironment:
    """必須設定を一括検証し、プロセス共通の実行環境を確定します。"""
    global _runtime_environment  # noqa: PLW0603 - Discord接続前に一度だけ確定する起動設定のため。
    values = os.environ if environ is None else environ
    errors = _validate_environment(values)
    if errors:
        details = "\n- ".join(errors)
        message = f"Invalid Bot environment configuration:\n- {details}"
        raise RuntimeError(message)

    environment = BotEnvironment(values[BOT_ENVIRONMENT_KEY].strip().casefold())
    api_usage_report_target_key = (
        DEBUG_API_USAGE_REPORT_TARGET_ID_KEY
        if environment is BotEnvironment.DEBUG
        else PRODUCTION_API_USAGE_REPORT_TARGET_ID_KEY
    )
    runtime = RuntimeEnvironment(
        environment=environment,
        chatbot_test_channel_ids=frozenset((int(values[DEBUG_CHATBOT_CHANNEL_ID_KEY]),)),
        api_usage_report_target_id=int(values[api_usage_report_target_key]),
    )
    _runtime_environment = runtime
    logger.info(
        "Configured Bot runtime environment (environment=%s, chatbot_test_channel_ids=%s)",
        runtime.environment.value,
        sorted(runtime.chatbot_test_channel_ids),
    )
    return runtime


def get_runtime_environment() -> RuntimeEnvironment:
    """起動時に検証済みの実行環境を返します。"""
    if _runtime_environment is None:
        message = "Bot runtime environment is not configured"
        raise RuntimeError(message)
    return _runtime_environment


def _validate_environment(environ: Mapping[str, str]) -> list[str]:
    """不足、形式不正、環境固有制約を利用者向けエラーとして列挙します。"""
    errors: list[str] = [f"{key} is required" for key in REQUIRED_ENVIRONMENT_KEYS if not environ.get(key, "").strip()]
    raw_environment = environ.get(BOT_ENVIRONMENT_KEY, "").strip().casefold()
    try:
        BotEnvironment(raw_environment)
    except ValueError:
        errors.append(f"{BOT_ENVIRONMENT_KEY} must be production or debug")

    for key in INTEGER_ENVIRONMENT_KEYS:
        value = environ.get(key, "").strip()
        if value and not value.isdecimal():
            errors.append(f"{key} must be a Discord ID")
    return errors
