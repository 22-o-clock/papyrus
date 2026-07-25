import unittest

from core.runtime_environment import BotEnvironment, configure_runtime_environment

PRODUCTION_REPORT_TARGET_ID = 2
DEBUG_REPORT_TARGET_ID = 8


def ensure(condition: object) -> None:
    """条件を満たさない場合にテストを失敗させます。"""
    if not condition:
        raise AssertionError


def ensure_contains(needle: str, haystack: str) -> None:
    """期待文字列を含まない場合にテストを失敗させます。"""
    if needle not in haystack:
        raise AssertionError


def valid_environment(**overrides: str) -> dict[str, str]:
    """起動環境検証テスト用の最小構成を返します。"""
    values = {
        "BOT_ENVIRONMENT": "production",
        "CHANNEL_ID_DEBUG_CHATBOT": "10",
        "CHANNEL_ID_LISTEN_ONLY_MEMBER": "4",
        "CHANNEL_ID_LOBBY": "5",
        "CHATBOT_SUPABASE_CONNECTION_STRING": "postgresql://chatbot",
        "DISCORD_BOT_TOKEN": "discord-token",
        "ENABLED_COGS": "all",
        "LASTFM_API_KEY": "lastfm-key",
        "OPENAI_ADMIN_API_KEY": "admin-key",
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_USAGE_PROJECT_ID": "project-id",
        "ROLE_ID_BOT_ADMIN": "3",
        "SERVER_ID": "7",
        "SUPABASE_CONNECTION_STRING": "postgresql://main",
        "THREAD_ID_ANTHYME_LOG": "1",
        "THREAD_ID_API_USAGE_REPORT": "2",
        "THREAD_ID_DEBUG_API_USAGE_REPORT": "8",
        "THREAD_ID_LOG": "6",
        "VOICEVOX_URL": "http://127.0.0.1:50021",
    }
    values.update(overrides)
    return values


class RuntimeEnvironmentTest(unittest.TestCase):
    def test_production_excludes_only_exact_test_channel_ids(self) -> None:
        runtime = configure_runtime_environment(valid_environment())

        ensure(runtime.environment is BotEnvironment.PRODUCTION)
        ensure(runtime.enabled_cogs[0] == "admin")
        ensure(runtime.enabled_cogs[-1] == "voicevox")
        ensure(runtime.api_usage_report_target_id == PRODUCTION_REPORT_TARGET_ID)
        ensure(not runtime.should_process_chatbot_channel(10))
        ensure(runtime.should_process_chatbot_channel(11))

    def test_debug_allows_only_exact_test_channel_ids(self) -> None:
        runtime = configure_runtime_environment(valid_environment(BOT_ENVIRONMENT="debug"))

        ensure(runtime.environment is BotEnvironment.DEBUG)
        ensure(runtime.api_usage_report_target_id == DEBUG_REPORT_TARGET_ID)
        ensure(runtime.should_process_chatbot_channel(10))
        ensure(not runtime.should_process_chatbot_channel(11))

    def test_selects_cogs_in_standard_load_order(self) -> None:
        runtime = configure_runtime_environment(valid_environment(ENABLED_COGS=" voicevox, chatbot,admin "))

        ensure(runtime.enabled_cogs == ("admin", "chatbot", "voicevox"))

    def test_none_selects_no_cogs(self) -> None:
        runtime = configure_runtime_environment(valid_environment(ENABLED_COGS="none"))

        ensure(runtime.enabled_cogs == ())

    def test_debug_requires_test_channel(self) -> None:
        try:
            configure_runtime_environment(
                valid_environment(BOT_ENVIRONMENT="debug", CHANNEL_ID_DEBUG_CHATBOT=""),
            )
        except RuntimeError as error:
            ensure_contains("CHANNEL_ID_DEBUG_CHATBOT is required", str(error))
            return
        raise AssertionError

    def test_reports_all_missing_required_values(self) -> None:
        try:
            configure_runtime_environment({"BOT_ENVIRONMENT": "production"})
        except RuntimeError as error:
            message = str(error)
            ensure_contains("DISCORD_BOT_TOKEN is required", message)
            ensure_contains("SUPABASE_CONNECTION_STRING is required", message)
            ensure_contains("THREAD_ID_API_USAGE_REPORT is required", message)
            ensure_contains("THREAD_ID_DEBUG_API_USAGE_REPORT is required", message)
            ensure_contains("ENABLED_COGS is required", message)
            return
        raise AssertionError

    def test_rejects_unknown_environment_and_invalid_ids(self) -> None:
        try:
            configure_runtime_environment(
                valid_environment(
                    BOT_ENVIRONMENT="staging",
                    CHANNEL_ID_DEBUG_CHATBOT="invalid",
                ),
            )
        except RuntimeError as error:
            message = str(error)
            ensure_contains("BOT_ENVIRONMENT must be production or debug", message)
            ensure_contains("CHANNEL_ID_DEBUG_CHATBOT must be a Discord ID", message)
            return
        raise AssertionError

    def test_rejects_unknown_duplicate_and_empty_cog_names(self) -> None:
        for enabled_cogs, expected_message in (
            ("chatbot,unknown", "ENABLED_COGS contains unknown Cog names: unknown"),
            ("chatbot,chatbot", "ENABLED_COGS contains duplicate Cog names: chatbot"),
            ("chatbot,,audit", "ENABLED_COGS must be all, none, or a comma-separated list of Cog names"),
        ):
            with self.subTest(enabled_cogs=enabled_cogs):
                try:
                    configure_runtime_environment(valid_environment(ENABLED_COGS=enabled_cogs))
                except RuntimeError as error:
                    ensure_contains(expected_message, str(error))
                    continue
                raise AssertionError
