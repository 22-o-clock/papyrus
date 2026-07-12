import datetime
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import discord
from discord import Message

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.responses_api import AttachmentInMemory, LLMMessage, ResponseAction, ResponsePipeline, ShortTermMemory

if TYPE_CHECKING:
    from openai import AsyncOpenAI


@dataclass
class MessageSpec:
    """テスト用Discordメッセージの可変要素を表します。"""

    message_id: int
    author_id: int
    author_name: str
    content: str
    mentioned_user_ids: tuple[int, ...] = ()
    reply_to_message_id: int | None = None


def make_message(spec: MessageSpec) -> Message:
    """短期記憶のテストに必要な属性だけを持つDiscordメッセージを作成します。"""
    message_type = discord.MessageType.reply if spec.reply_to_message_id is not None else discord.MessageType.default
    reference = SimpleNamespace(message_id=spec.reply_to_message_id) if spec.reply_to_message_id is not None else None
    message = SimpleNamespace(
        id=spec.message_id,
        author=SimpleNamespace(id=spec.author_id, display_name=spec.author_name),
        clean_content=spec.content,
        created_at=datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC),
        message_snapshots=[],
        type=message_type,
        reference=reference,
        mentions=[SimpleNamespace(id=user_id) for user_id in spec.mentioned_user_ids],
        attachments=[],
        channel=SimpleNamespace(id=100),
        guild=SimpleNamespace(id=200),
    )
    return cast("Message", message)


class ShortTermMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_distinct_user_ids_for_same_display_name(self) -> None:
        memory = ShortTermMemory()
        await memory.append(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="同じ名前", content="一人目の発言"))
        )
        await memory.append(
            make_message(MessageSpec(message_id=2, author_id=20, author_name="同じ名前", content="二人目の発言"))
        )

        serialized = json.loads(memory.to_json())

        if [message["author_id"] for message in serialized] != [10, 20]:
            self.fail("同じ表示名の発言者がユーザーIDで区別されていません")
        if [message["author_name"] for message in serialized] != ["同じ名前", "同じ名前"]:
            self.fail("表示名が会話生成用の情報として保持されていません")

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
        if memory.can_target_message(2):
            self.fail("参考情報の投稿を返信またはリアクションの対象にできます")

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

        serialized = json.loads(memory.to_json())

        if serialized[0]["attachments"] != [
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

        serialized = json.loads(memory.to_json())
        attachment = serialized[0]["attachments"][0]

        if attachment != {
            "attachment_id": 100,
            "filename": "poster.png",
            "kind": "image",
            "analysis_status": "pending",
        }:
            self.fail("解析中の添付に未完成の要約や重要テキストが含まれています")


class LLMMessageTest(unittest.TestCase):
    def test_serializes_reply_target_as_message_id(self) -> None:
        reply_to_message_id = 123
        response = LLMMessage(
            action=ResponseAction.REPLY,
            content="返信です",
            reply_to_message_id=reply_to_message_id,
        )

        serialized = json.loads(response.to_json(bot_name="Papyrus"))

        if serialized["reply_to_message_id"] != reply_to_message_id:
            self.fail("生成結果の返信先がメッセージIDとして出力されていません")
        if "reply_to" in serialized:
            self.fail("表示名ベースの返信先が生成結果に残っています")

    def test_accepts_silence_without_content(self) -> None:
        response = LLMMessage(action=ResponseAction.SILENCE)

        if response.content:
            self.fail("沈黙行動に不要な本文が設定されています")

    def test_accepts_reaction_with_target_and_emoji(self) -> None:
        response = LLMMessage(
            action=ResponseAction.REACTION,
            reply_to_message_id=123,
            reaction_emoji="👍",
        )

        if response.reaction_emoji != "👍":
            self.fail("リアクション絵文字を保持できません")


class FakeResponses:
    """一段階生成のAPI呼び出し回数を記録します。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        """最終回答を返し、呼び出し引数を保存します。"""
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=LLMMessage(action=ResponseAction.MESSAGE, content="短い返答"))


class ResponsePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_generates_final_response_with_one_model_call(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="起きた"))
        )

        generated = await pipeline.generate_response(ChannelRole.CHAT, is_unanswered_question=False)

        if generated.content != "短い返答":
            self.fail("一段階で生成した回答が最終結果になっていません")
        if len(responses.calls) != 1:
            self.fail("最終回答の生成でモデルが複数回呼び出されています")
        if responses.calls[0]["model"] != "gpt-5.6-sol":
            self.fail("最終回答がSolで生成されていません")
