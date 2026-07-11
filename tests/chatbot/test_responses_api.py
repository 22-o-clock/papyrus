import datetime
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import discord
from discord import Message

from cogs.chatbot.responses_api import LLMMessage, ResponseAction, ShortTermMemory


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
