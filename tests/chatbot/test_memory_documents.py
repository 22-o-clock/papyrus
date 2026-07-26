import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from cogs.chatbot.responses_api import MessageInMemory
from cogs.chatbot.use_cases.long_term_memory import LongTermMemoryUseCases
from cogs.chatbot.use_cases.memory_search import MemorySearchUseCases


class MemoryResponseContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_selects_authors_mentions_replies_and_resolved_aliases(self) -> None:
        channel_id = 50
        prompt_messages = [
            MessageInMemory(
                message_id=1,
                author_id=10,
                author_name="発言者",
                content="別の人についての発言",
                reply_to_message_id=99,
                mentioned_user_ids=[20],
                timestamp=datetime(2026, 7, 26, tzinfo=UTC),
            )
        ]
        short_term_memory = SimpleNamespace(
            memory=prompt_messages,
            get_prompt_messages=Mock(return_value=prompt_messages),
        )
        document_repository = SimpleNamespace(
            get_for_users=AsyncMock(
                return_value=[
                    SimpleNamespace(document_key="bot", content="Bot文書"),
                    SimpleNamespace(document_key="shared", content="共有文書"),
                ]
            )
        )
        message_repository = SimpleNamespace(get_by_ids=AsyncMock(return_value=[SimpleNamespace(author_id=30)]))
        use_cases = MemorySearchUseCases(
            cast("Any", SimpleNamespace(user=SimpleNamespace(id=999))),
            cast("Any", {channel_id: SimpleNamespace(short_term_memory=short_term_memory)}),
            cast("Any", document_repository),
            cast("Any", SimpleNamespace()),
            cast("Any", message_repository),
        )

        context = await use_cases.build_response_context(channel_id, {"別名": 40})

        document_repository.get_for_users.assert_awaited_once_with({10, 20, 30, 40})
        if "## bot\nBot文書" not in context or "## shared\n共有文書" not in context:
            self.fail("Bot文書と共有文書を回答文脈へ整形できていません")


class MemorySourceExclusionTest(unittest.IsolatedAsyncioTestCase):
    def test_excluded_message_is_not_memory_evidence_or_update_source(self) -> None:
        message = cast(
            "Any",
            SimpleNamespace(
                is_long_term_memory_excluded=True,
                is_forwarded=False,
                is_bot=True,
                is_self=True,
            ),
        )
        use_cases = object.__new__(LongTermMemoryUseCases)

        if use_cases._is_memory_evidence(message):  # noqa: SLF001
            self.fail("自動生成メッセージを長期記憶の根拠に含めています")
        if use_cases._is_update_source(message):  # noqa: SLF001
            self.fail("除外フラグ付きメッセージを長期記憶の更新起点にしています")

    async def test_excluded_message_is_serialized_as_automatically_generated_context(self) -> None:
        message = cast(
            "Any",
            SimpleNamespace(
                message_id=1,
                author_id=999,
                author_name="Papyrus",
                content="予定を開始します。",
                reply_to_message_id=None,
                mentioned_user_ids=[],
                created_at=datetime(2026, 7, 26, tzinfo=UTC),
                is_bot=True,
                is_self=True,
                is_forwarded=False,
                is_long_term_memory_excluded=True,
                custom_profile_name=None,
                embeds=[],
            ),
        )
        use_cases = object.__new__(LongTermMemoryUseCases)
        use_cases._messages = cast("Any", SimpleNamespace(get_attachments=AsyncMock(return_value=[])))  # noqa: SLF001

        payload = await use_cases._serialize_messages([message])  # noqa: SLF001

        if payload[0]["g"] is not True:
            self.fail("自動生成メッセージを文脈用の印付きで渡していません")
        if "c" not in payload[0] or "r" in payload[0] or "u" in payload[0] or "e" in payload[0] or "x" in payload[0]:
            self.fail("長期記憶用メッセージが疎なコンパクト形式ではありません")
