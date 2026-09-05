import datetime
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import discord
from discord import Message

from cogs.chatbot import observability
from cogs.chatbot.responses_api import (
    MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS,
    MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS,
    AttachmentInMemory,
    MemoryDocumentShortenResult,
    MemoryDocumentUpdater,
    MemoryDocumentUpdateResult,
    MessageInMemory,
    ReactionInMemory,
    ReactionUserInMemory,
    ShortTermMemory,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI

FUNCTION_CALL_RESPONSE_COUNT = 2


@dataclass
class MessageSpec:
    """テスト用Discordメッセージの可変要素を表します。"""

    message_id: int
    author_id: int
    author_name: str
    content: str
    mentioned_user_ids: tuple[int, ...] = ()
    reply_to_message_id: int | None = None
    forwarded_content: str | None = None


def make_message(spec: MessageSpec) -> Message:
    """短期記憶のテストに必要な属性だけを持つDiscordメッセージを作成します。"""
    message_type = discord.MessageType.reply if spec.reply_to_message_id is not None else discord.MessageType.default
    reference = SimpleNamespace(message_id=spec.reply_to_message_id) if spec.reply_to_message_id is not None else None
    message = SimpleNamespace(
        id=spec.message_id,
        author=SimpleNamespace(id=spec.author_id, display_name=spec.author_name),
        clean_content=spec.content,
        created_at=datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC),
        message_snapshots=([SimpleNamespace(content=spec.forwarded_content)] if spec.forwarded_content is not None else []),
        type=message_type,
        reference=reference,
        mentions=[SimpleNamespace(id=user_id) for user_id in spec.mentioned_user_ids],
        attachments=[],
        embeds=[],
        channel=SimpleNamespace(id=100),
        guild=SimpleNamespace(id=200),
    )
    return cast("Message", message)


class ShortTermMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_discord_embed_metadata(self) -> None:
        memory = ShortTermMemory()
        message = make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="URL"))
        embed = discord.Embed(
            title="展開されたタイトル",
            description="展開された説明",
            url="https://example.com/post",
        )
        embed.set_author(name="投稿者")
        embed.add_field(name="項目", value="値")
        cast("object", message).__setattr__("embeds", [embed])

        await memory.append(message)

        serialized = json.loads(memory.to_json())["m"][0]["e"][0]
        if (
            serialized["title"] != "展開されたタイトル"
            or serialized["description"] != "展開された説明"
            or serialized["author"] != "投稿者"
            or serialized["fields"] != [{"name": "項目", "value": "値"}]
        ):
            self.fail("Discord Embedの本文情報を短期文脈へ保存できていません")

    async def test_marks_forwarded_content_with_sparse_flag(self) -> None:
        memory = ShortTermMemory()
        await memory.append(
            make_message(
                MessageSpec(
                    message_id=1,
                    author_id=10,
                    author_name="転送者",
                    content="",
                    forwarded_content="原文です",
                )
            )
        )

        serialized = json.loads(memory.to_json())["m"][0]

        if serialized.get("f") is not True or serialized.get("c") != "原文です":
            self.fail("転送メッセージを本文への説明追加ではなく明示フラグで表現できていません")

    async def test_serializes_distinct_user_ids_for_same_display_name(self) -> None:
        memory = ShortTermMemory()
        await memory.append(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="同じ名前", content="一人目の発言"))
        )
        await memory.append(
            make_message(MessageSpec(message_id=2, author_id=20, author_name="同じ名前", content="二人目の発言"))
        )

        serialized = json.loads(memory.to_json())

        if [message["a"] for message in serialized["m"]] != [10, 20]:
            self.fail("同じ表示名の発言者がユーザーIDで区別されていません")
        if serialized["a"] != {"10": "同じ名前", "20": "同じ名前"}:
            self.fail("表示名が発言者辞書に保持されていません")

    async def test_keeps_reply_message_id_and_all_mentioned_user_ids(self) -> None:
        memory = ShortTermMemory()
        author_id = 30
        await memory.append(
            make_message(
                MessageSpec(
                    message_id=3,
                    author_id=author_id,
                    author_name="発言者",
                    content="返信内容",
                    mentioned_user_ids=(40, 50),
                    reply_to_message_id=1,
                )
            )
        )

        stored = memory.memory[0]

        if stored.reply_to_message_id != 1:
            self.fail("返信先のメッセージIDが保持されていません")
        if stored.mentioned_user_ids != [40, 50]:
            self.fail("すべてのメンション先ユーザーIDが保持されていません")
        if not memory.contains_message(3):
            self.fail("保存済みメッセージを検出できません")
        if memory.contains_message(999):
            self.fail("未保存のメッセージを誤検出しています")
        if memory.get_author_id(3) != author_id:
            self.fail("メッセージIDから発言者IDを取得できません")
        if memory.get_author_id(999) is not None:
            self.fail("未保存メッセージに発言者IDを返しています")

    async def test_reset_keeps_only_last_message_as_stale_context(self) -> None:
        memory = ShortTermMemory()
        await memory.append(make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者A", content="古い話題")))
        await memory.append(make_message(MessageSpec(message_id=2, author_id=20, author_name="発言者B", content="最後の投稿")))

        memory.reset_for_new_conversation()

        if [message.message_id for message in memory.memory] != [2]:
            self.fail("会話リセット後に最後の投稿以外が残っています")
        if not memory.memory[0].is_stale_context:
            self.fail("会話リセット後の最後の投稿が参考情報として扱われていません")

    def test_restore_reapplies_latest_twelve_hour_conversation_gap(self) -> None:
        memory = ShortTermMemory()
        first_timestamp = datetime.datetime(2026, 7, 22, 9, 0, tzinfo=datetime.UTC)
        memory.restore(
            [
                MessageInMemory(1, 10, "人間A", "古い話題", None, [], first_timestamp),
                MessageInMemory(
                    2,
                    99,
                    "Bot",
                    "古い話題への返答",
                    None,
                    [],
                    first_timestamp + datetime.timedelta(minutes=1),
                    is_bot=True,
                ),
                MessageInMemory(
                    3,
                    20,
                    "人間B",
                    "新しい話題",
                    None,
                    [],
                    first_timestamp + datetime.timedelta(hours=13),
                ),
                MessageInMemory(
                    4,
                    30,
                    "人間C",
                    "新しい話題の続き",
                    None,
                    [],
                    first_timestamp + datetime.timedelta(hours=14),
                ),
            ]
        )

        if [message.message_id for message in memory.memory] != [2, 3, 4]:
            self.fail("再起動後に12時間の会話区切りより前の履歴が復活しています")
        if not memory.memory[0].is_stale_context:
            self.fail("区切り直前の投稿が古い参考情報として復元されていません")

    async def test_serializes_completed_attachment_analysis_within_message_context(self) -> None:
        memory = ShortTermMemory()
        await memory.append(make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="添付あり")))
        memory.set_attachment_analysis(
            1,
            AttachmentInMemory(
                attachment_id=100,
                filename="poster.png",
                kind="image",
                analysis_status="completed",
                summary="新入生歓迎会のポスター",
                important_text="4/13 15:15 美術室",
            ),
        )

        serialized = json.loads(memory.to_json())["m"]

        if serialized[0]["x"] != [
            {
                "attachment_id": 100,
                "filename": "poster.png",
                "kind": "image",
                "analysis_status": "completed",
                "summary": "新入生歓迎会のポスター",
                "important_text": "4/13 15:15 美術室",
            }
        ]:
            self.fail("完了済みの添付解析結果が会話文脈に含まれていません")

    async def test_serializes_pending_attachment_without_unfinished_analysis_text(self) -> None:
        memory = ShortTermMemory()
        await memory.append(make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="添付あり")))
        memory.set_attachment_analysis(
            1,
            AttachmentInMemory(
                attachment_id=100,
                filename="poster.png",
                kind="image",
                analysis_status="pending",
            ),
        )

        serialized = json.loads(memory.to_json())["m"]
        attachment = serialized[0]["x"][0]

        if attachment != {
            "attachment_id": 100,
            "filename": "poster.png",
            "kind": "image",
            "analysis_status": "pending",
        }:
            self.fail("解析中の添付に未完成の要約や重要テキストが含まれています")

    async def test_serializes_reactions_only_for_response_context(self) -> None:
        reactor_user_id = 20
        memory = ShortTermMemory()
        await memory.append(make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="了解")))
        memory.set_reactions(
            1,
            [
                ReactionInMemory(
                    emoji_name="👍",
                    emoji_id=None,
                    animated=False,
                    reaction_type="normal",
                    count=2,
                    reactors=[ReactionUserInMemory(user_id=reactor_user_id, display_name="反応者", is_bot=False)],
                    reactors_incomplete=True,
                )
            ],
        )

        response_context = json.loads(memory.to_json())["m"][0]
        memory_context = memory.memory[0].to_dict(include_reactions=False)

        if response_context["q"][0]["reactors"][0]["user_id"] != reactor_user_id:
            self.fail("発言生成用の文脈にリアクションした人物が含まれていません")
        if not response_context["q"][0]["reactors_incomplete"]:
            self.fail("リアクション利用者の不完全取得状態が保持されていません")
        if "reactions" in memory_context:
            self.fail("長期記憶用に除外したリアクションが残っています")

    async def test_omits_empty_default_fields_from_prompt_context(self) -> None:
        memory = ShortTermMemory()
        await memory.append(make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="了解")))

        message = json.loads(memory.to_json())["m"][0]

        if set(message) != {"i", "a", "t", "c"}:
            self.fail(f"空または既定値の項目がモデル入力に残っています: {set(message)}")

    async def test_compact_context_uses_materially_fewer_tokens_than_legacy_json(self) -> None:
        memory = ShortTermMemory()
        for message_id in range(1, 11):
            await memory.append(
                make_message(
                    MessageSpec(
                        message_id=message_id,
                        author_id=10 + message_id % 2,
                        author_name="発言者",
                        content="短い会話です",
                    )
                )
            )

        compact_tokens = len(memory.encoding.encode(memory.to_json()))
        legacy_json = json.dumps([message.to_dict() for message in memory.memory], ensure_ascii=False, indent=2)
        legacy_tokens = len(memory.encoding.encode(legacy_json))

        if compact_tokens >= legacy_tokens * 0.6:
            self.fail(f"会話入力を十分に圧縮できていません: compact={compact_tokens}, legacy={legacy_tokens}")


class ConfigRecordingResponses:
    """記憶処理のAPI設定を記録します。"""

    def __init__(self, output_parsed: object) -> None:
        self.output_parsed = output_parsed
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        """呼び出し引数を保存して、指定された構造化結果を返します。"""
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.output_parsed)


class MemoryModelConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_updates_memory_documents_with_luna_and_medium_reasoning(self) -> None:
        responses = ConfigRecordingResponses(MemoryDocumentUpdateResult())
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        usage_repository = AsyncMock()
        expected_channel_count = 2

        with patch.object(observability, "_usage_repository", usage_repository):
            await MemoryDocumentUpdater(client).update({"channel_conversations": [{}] * expected_channel_count})

        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("長期記憶文書の更新がLunaを使用していません")
        if responses.calls[0]["reasoning"] != {"effort": "medium"}:
            self.fail("長期記憶文書の更新の推論強度がmediumになっていません")
        if responses.calls[0]["metadata"] != {"operation": "memory_document_update"}:
            self.fail("長期記憶文書の更新種別をPlatformのmetadataへ設定していません")
        if usage_repository.add.await_args.args[0].item_count != expected_channel_count:
            self.fail("API利用量へ更新対象のチャンネル数を記録していません")

    async def test_uses_separate_instructions_for_shortening_retry(self) -> None:
        responses = ConfigRecordingResponses(MemoryDocumentUpdateResult())
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        usage_repository = AsyncMock()
        expected_document_count = 3

        with patch.object(observability, "_usage_repository", usage_repository):
            await MemoryDocumentUpdater(client).shorten({"documents": [{}] * expected_document_count})

        if responses.calls[0]["instructions"] != MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS:
            self.fail("文字数超過時に独立した短縮専用の指示を使用していません")
        if MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS in str(responses.calls[0]["instructions"]):
            self.fail("短縮callへ通常更新の指示を重複送信しています")
        if responses.calls[0]["metadata"] != {"operation": "memory_document_shorten"}:
            self.fail("短縮処理の種別をPlatformのmetadataへ設定していません")
        if responses.calls[0]["text_format"] is not MemoryDocumentShortenResult:
            self.fail("短縮callが本文以外の識別情報も再生成する出力形式になっています")
        if usage_repository.add.await_args.args[0].item_count != expected_document_count:
            self.fail("API利用量へ短縮対象の文書数を記録していません")
