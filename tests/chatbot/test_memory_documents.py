import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from cogs.chatbot.responses_api import (
    MemberAliasCandidate,
    MemoryDocumentShortenResult,
    MemoryDocumentUpdate,
    MemoryDocumentUpdateResult,
    MessageInMemory,
)
from cogs.chatbot.use_cases.long_term_memory import (
    LongTermMemoryUseCases,
    MemoryDocumentValidationError,
)
from cogs.chatbot.use_cases.memory_search import MemorySearchUseCases

EXPECTED_PERSON_TARGET_CHARACTERS = 1000
ONE_HOUR_SECONDS = 3600


def person_document(character_count: int) -> str:
    headings = "# 人物の記憶\n\n## 基本情報\n\n## 嗜好・関心\n\n## 関係・継続事項\n\n"
    return headings + ("記" * (character_count - len(headings)))


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

    async def test_builds_pending_index_without_exposing_content_until_tool_use(self) -> None:
        channel_id = 50
        prompt_messages = [
            MessageInMemory(
                message_id=1,
                author_id=10,
                author_name="発言者",
                content="現在の会話",
                reply_to_message_id=None,
                mentioned_user_ids=[],
                timestamp=datetime(2026, 7, 27, tzinfo=UTC),
            )
        ]
        pending_message = SimpleNamespace(
            message_id=2,
            channel_id=60,
            author_id=20,
            author_name="別の発言者",
            content="未反映の重要情報",
            reply_to_message_id=None,
            mentioned_user_ids=[],
            created_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
            is_bot=False,
            is_self=False,
            is_forwarded=False,
            is_long_term_memory_excluded=False,
        )
        repository = SimpleNamespace(
            get_response_snapshot=AsyncMock(
                return_value=(
                    [SimpleNamespace(document_key="shared", content="共有文書")],
                    [pending_message],
                )
            )
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_channel=Mock(return_value=SimpleNamespace(name="別チャンネル")),
        )
        use_cases = MemorySearchUseCases(
            cast("Any", bot),
            cast(
                "Any",
                {
                    channel_id: SimpleNamespace(
                        short_term_memory=SimpleNamespace(
                            get_prompt_messages=Mock(return_value=prompt_messages),
                        )
                    )
                },
            ),
            cast("Any", repository),
            cast("Any", SimpleNamespace()),
            cast("Any", SimpleNamespace(get_by_ids=AsyncMock(return_value=[]))),
        )

        context = await use_cases.build_response_memory(channel_id)

        if "未反映の重要情報" in context.pending_index or "別チャンネル" not in context.pending_index:
            self.fail("判断用索引へ本文を露出しているか、チャンネル情報が不足しています")
        if "未反映の重要情報" not in context.pending_context:
            self.fail("Function tool用の未反映本文を構築できていません")
        repository.get_response_snapshot.assert_awaited_once_with({10}, exclude_channel_id=channel_id)


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


class MemoryDocumentBatchingTest(unittest.IsolatedAsyncioTestCase):
    async def test_enqueues_one_server_batch_at_fifty_pending_messages(self) -> None:
        messages = [
            SimpleNamespace(
                message_id=index,
                is_long_term_memory_excluded=False,
                is_forwarded=False,
                is_bot=False,
                is_self=False,
            )
            for index in range(1, 51)
        ]
        repository = SimpleNamespace(
            get_pending_messages=AsyncMock(return_value=messages),
            enqueue=AsyncMock(),
        )
        use_cases = object.__new__(LongTermMemoryUseCases)
        use_cases._documents = cast("Any", repository)  # noqa: SLF001
        use_cases._message_token_count = Mock(return_value=1)  # type: ignore[method-assign]  # noqa: SLF001
        use_cases._schedule_worker = Mock()  # type: ignore[method-assign]  # noqa: SLF001

        await use_cases._enqueue_if_threshold_reached(50)  # noqa: SLF001

        repository.enqueue.assert_awaited_once_with(
            0,
            50,
            "message_count",
            wait_for_attachments=True,
        )

    async def test_groups_multiple_channels_in_one_update_payload(self) -> None:
        def message(message_id: int, channel_id: int, author_name: str) -> SimpleNamespace:
            return SimpleNamespace(
                message_id=message_id,
                channel_id=channel_id,
                author_id=message_id,
                author_name=author_name,
                content=f"{author_name}の発言",
                reply_to_message_id=None,
                mentioned_user_ids=[],
                created_at=datetime(2026, 7, 27, message_id, tzinfo=UTC),
                is_bot=False,
                is_self=False,
                is_forwarded=False,
                is_long_term_memory_excluded=False,
                custom_profile_name=None,
                embeds=[],
            )

        use_cases = object.__new__(LongTermMemoryUseCases)
        use_cases._bot = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(
                get_channel=lambda channel_id: SimpleNamespace(name=f"channel-{channel_id}"),
            ),
        )
        use_cases._messages = cast(  # noqa: SLF001
            "Any",
            SimpleNamespace(get_attachments=AsyncMock(return_value=[])),
        )

        payload = await use_cases._build_update_payload(  # noqa: SLF001
            [cast("Any", message(1, 10, "一人目")), cast("Any", message(2, 20, "二人目"))],
            [],
            [],
            [],
        )

        conversations = payload["channel_conversations"]
        if not isinstance(conversations, list) or [item["channel_id"] for item in conversations] != [10, 20]:
            self.fail("複数チャンネルを一つの更新ペイロード内で分離できていません")

    def test_daily_flush_runs_at_next_four_am_jst(self) -> None:
        now = datetime(2026, 7, 27, 18, tzinfo=UTC)

        seconds = LongTermMemoryUseCases._seconds_until_daily_flush(now)  # noqa: SLF001

        if seconds != ONE_HOUR_SECONDS:
            self.fail("午前4時の次回flush時刻をJSTで計算できていません")


class MemoryDocumentShorteningTest(unittest.IsolatedAsyncioTestCase):
    async def test_shortens_only_document_over_trigger_and_preserves_aliases(self) -> None:
        original_person = MemoryDocumentUpdate(
            document_key="person:10",
            document_type="person",
            target_user_id=10,
            content=person_document(2001),
        )
        unchanged_person = MemoryDocumentUpdate(
            document_key="person:20",
            document_type="person",
            target_user_id=20,
            content=person_document(500),
        )
        alias = MemberAliasCandidate(alias="呼び名", target_user_id=10, evidence_message_ids=[1])
        shortened_person = original_person.model_copy(update={"content": person_document(2200)})
        updater = SimpleNamespace(
            shorten=AsyncMock(
                return_value=MemoryDocumentShortenResult(
                    contents=[shortened_person.content],
                ),
            )
        )
        use_cases = object.__new__(LongTermMemoryUseCases)
        use_cases._updater = cast("Any", updater)  # noqa: SLF001

        result = await use_cases._shorten_documents_if_needed(  # noqa: SLF001
            MemoryDocumentUpdateResult(
                updates=[original_person, unchanged_person],
                aliases=[alias],
            )
        )

        payload = updater.shorten.await_args.args[0]
        if set(payload) != {"documents"} or len(payload["documents"]) != 1:
            self.fail("短縮対象以外の文脈を短縮callへ渡しています")
        target = payload["documents"][0]
        if set(target) != {"content", "target_characters", "required_headings"}:
            self.fail("短縮出力に不要な識別情報を短縮callへ渡しています")
        if target["content"] != original_person.content:
            self.fail("文字数超過した人物文書だけを短縮対象にできていません")
        if target["required_headings"] != ("# 人物の記憶", "## 基本情報", "## 嗜好・関心", "## 関係・継続事項"):
            self.fail("対象の人物文書に必要な見出しだけを渡していません")
        if target["target_characters"] != EXPECTED_PERSON_TARGET_CHARACTERS:
            self.fail("人物文書の目標文字数が正しくありません")
        if result.updates != [shortened_person, unchanged_person] or result.aliases != [alias]:
            self.fail("短縮結果の部分マージまたは元の別名候補の保持に失敗しています")

        use_cases._validate_documents(result, {10, 20})  # noqa: SLF001

    async def test_does_not_shorten_person_document_at_trigger(self) -> None:
        updater = SimpleNamespace(shorten=AsyncMock())
        use_cases = object.__new__(LongTermMemoryUseCases)
        use_cases._updater = cast("Any", updater)  # noqa: SLF001
        result = MemoryDocumentUpdateResult(
            updates=[
                MemoryDocumentUpdate(
                    document_key="person:10",
                    document_type="person",
                    target_user_id=10,
                    content=person_document(2000),
                )
            ]
        )

        returned = await use_cases._shorten_documents_if_needed(result)  # noqa: SLF001

        if returned is not result:
            self.fail("短縮不要な結果を作り直しています")
        updater.shorten.assert_not_awaited()

    def test_uses_same_limits_for_every_document_type(self) -> None:
        use_cases = object.__new__(LongTermMemoryUseCases)

        for document_type in ("person", "bot", "shared"):
            if use_cases._document_character_limits(document_type) != (1000, 2000, 2500):  # noqa: SLF001
                self.fail(f"{document_type}文書の目標・短縮開始・強制拒否上限が統一されていません")

    def test_rejects_person_document_over_hard_maximum(self) -> None:
        use_cases = object.__new__(LongTermMemoryUseCases)
        result = MemoryDocumentUpdateResult(
            updates=[
                MemoryDocumentUpdate(
                    document_key="person:10",
                    document_type="person",
                    target_user_id=10,
                    content=person_document(2501),
                )
            ]
        )

        try:
            use_cases._validate_documents(result, {10})  # noqa: SLF001
        except MemoryDocumentValidationError:
            pass
        else:
            self.fail("強制拒否上限を超えた人物文書を受理しています")
