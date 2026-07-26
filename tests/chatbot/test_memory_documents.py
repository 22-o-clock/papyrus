import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from cogs.chatbot.responses_api import MessageInMemory
from cogs.chatbot.services.memory_migration import parse_memory_migration_markdown
from cogs.chatbot.use_cases.long_term_memory import LongTermMemoryUseCases
from cogs.chatbot.use_cases.memory_search import MemorySearchUseCases


class MemoryMigrationMarkdownTest(unittest.TestCase):
    def test_parses_shared_bot_and_person_documents(self) -> None:
        result = parse_memory_migration_markdown(
            """# Chatbot long-term memory migration
<!-- document:shared -->
# 共有記憶
## 共有されている前提
共有事項です。
## 継続中の話題・決定
<!-- document:bot -->
# Papyrusの自己記憶
## 嗜好・立場
私は簡潔な会話を好みます。
## 人物への印象・関係
## 継続的な約束
<!-- document:person:123 -->
# 人物の記憶
## 基本情報
この人物についての記憶です。
## 嗜好・関心
## 関係・継続事項
"""
        )

        if result != {
            "shared": "# 共有記憶\n## 共有されている前提\n共有事項です。\n## 継続中の話題・決定",
            "bot": ("# Papyrusの自己記憶\n## 嗜好・立場\n私は簡潔な会話を好みます。\n## 人物への印象・関係\n## 継続的な約束"),
            "person:123": "# 人物の記憶\n## 基本情報\nこの人物についての記憶です。\n## 嗜好・関心\n## 関係・継続事項",
        }:
            self.fail("移行Markdownを文書単位に分解できていません")

    def test_rejects_duplicate_document_heading(self) -> None:
        try:
            parse_memory_migration_markdown("<!-- document:shared -->\nA\n<!-- document:shared -->\nB\n<!-- document:bot -->\n")
        except ValueError:
            return
        self.fail("重複した文書見出しを拒否していません")

    def test_rejects_over_limit_person_document(self) -> None:
        try:
            parse_memory_migration_markdown(
                "<!-- document:shared -->\n# 共有記憶\n## 共有されている前提\n## 継続中の話題・決定\n"
                "<!-- document:bot -->\n# Papyrusの自己記憶\n## 嗜好・立場\n## 人物への印象・関係\n## 継続的な約束\n"
                f"<!-- document:person:123 -->\n# 人物の記憶\n## 基本情報\n{'あ' * 1001}\n"
                "## 嗜好・関心\n## 関係・継続事項"
            )
        except ValueError:
            return
        self.fail("上限を超える人物文書を拒否していません")

    def test_rejects_unknown_document_type(self) -> None:
        try:
            parse_memory_migration_markdown(
                "<!-- document:shared -->\n# 共有記憶\n## 共有されている前提\n## 継続中の話題・決定\n"
                "<!-- document:bot -->\n# Papyrusの自己記憶\n## 嗜好・立場\n## 人物への印象・関係\n## 継続的な約束\n"
                "<!-- document:external:example -->\n本文"
            )
        except ValueError:
            return
        self.fail("未対応の文書種別を拒否していません")


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
