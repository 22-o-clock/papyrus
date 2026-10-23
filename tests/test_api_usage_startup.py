from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.api_usage.use_cases import reporting
from cogs.chatbot import chatbot, observability


class ApiUsageStartupTest(IsolatedAsyncioTestCase):
    async def test_chatbot_alone_records_usage_before_and_after_reconnection(self) -> None:
        """レポートCogがなくても、初回起動と再接続後の呼び出しを保存します。"""
        repository = SimpleNamespace(initialize_measurement=AsyncMock(), add=AsyncMock())
        conversation = MagicMock()

        async def ready() -> None:
            repository.initialize_measurement.assert_awaited()

        conversation.on_ready = AsyncMock(side_effect=ready)
        session_factory = MagicMock()
        with (
            patch.object(observability, "_usage_repository", None),
            patch.object(observability, "ChatbotApiUsageRepository", return_value=repository) as repository_class,
            patch.object(chatbot, "get_runtime_environment"),
            patch.object(chatbot, "ConversationUseCases", return_value=conversation),
            patch.object(chatbot, "AliasExcelManagementUseCases"),
        ):
            cog = chatbot.ChatBot(MagicMock(), session_factory)
            repository_class.assert_called_once_with(session_factory)
            for _ in range(2):
                await cog.on_ready()
                request = AsyncMock(return_value=SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10)))
                await observability.observe_chatbot_api_call("reply_generation", "gpt-6-luna", request())

        expected_calls = 2
        if repository.add.await_count != expected_calls:
            self.fail("Chatbot単独起動時のAPI利用量が保存されていません")
        expected_usage = (True, 100, 10)
        for call in repository.add.await_args_list:
            increment = call.args[0]
            if (increment.succeeded, increment.input_tokens, increment.output_tokens) != expected_usage:
                self.fail("API利用量の保存内容が実際の呼び出し結果と一致しません")

    async def test_reporter_does_not_replace_chatbot_measurement_repository(self) -> None:
        """レポート機能を読み込んでもChatbotの計測保存先を変更しません。"""
        measurement_repository = MagicMock()
        session_factory = MagicMock()
        with (
            patch.object(observability, "_usage_repository", measurement_repository),
            patch.object(reporting, "get_runtime_environment"),
            patch.dict(
                "os.environ",
                ROLE_ID_BOT_ADMIN="1",
                OPENAI_ADMIN_API_KEY="test-key",
                OPENAI_USAGE_PROJECT_ID="test-project",
            ),
            patch.object(reporting, "ChatbotApiUsageRepository") as report_repository,
        ):
            reporting.ApiUsageReportUseCases(MagicMock(), session_factory)
            report_repository.assert_called_once_with(session_factory)
            if observability._usage_repository is not measurement_repository:  # noqa: SLF001
                self.fail("レポート機能がChatbotの計測保存先を変更しています")
