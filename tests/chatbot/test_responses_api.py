import datetime
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import discord
from discord import Message

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.constants import SHORT_TERM_MEMORY_PROMPT_TOKENS
from cogs.chatbot.models import (
    CustomProfile,
    ResponseJudgment,
    ResponseMode,
)
from cogs.chatbot.responses_api import (
    MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS,
    MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS,
    PENDING_OTHER_CHANNEL_TOOL_NAME,
    RESPONSE_JUDGMENT_TIMEOUT_SECONDS,
    AttachmentInMemory,
    GeneratedReactionResponse,
    GeneratedRequiredReply,
    GeneratedTextResponse,
    LLMMessage,
    MemoryDocumentShortenResult,
    MemoryDocumentUpdater,
    MemoryDocumentUpdateResult,
    MessageInMemory,
    ReactionInMemory,
    ReactionUserInMemory,
    ResponseAction,
    ResponseGenerationOptions,
    ResponsePipeline,
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
        if memory.can_target_message(2):
            self.fail("参考情報の投稿を返信またはリアクションの対象にできます")

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

    def test_prompt_context_is_limited_without_discarding_retained_history(self) -> None:
        memory = ShortTermMemory()
        timestamp = datetime.datetime(2026, 7, 22, 9, 0, tzinfo=datetime.UTC)
        memory.memory = [
            MessageInMemory(
                message_id,
                10,
                "発言者",
                f"{message_id}:" + "会話の本文です。" * 120,
                None,
                [],
                timestamp + datetime.timedelta(minutes=message_id),
            )
            for message_id in range(1, 9)
        ]
        memory.forget()
        retained_ids = [message.message_id for message in memory.memory]

        prompt_json = memory.to_prompt_json()
        prompt_ids = [message["i"] for message in json.loads(prompt_json)["m"]]

        if len(memory.encoding.encode(prompt_json)) > SHORT_TERM_MEMORY_PROMPT_TOKENS:
            self.fail("モデルへ渡す短期記憶が2,000トークンを超えています")
        if prompt_ids == retained_ids:
            self.fail("内部保持履歴とモデル送信用履歴が分離されていません")
        if prompt_ids[-1] != retained_ids[-1]:
            self.fail("モデル送信用履歴から最新の投稿が欠落しています")
        if [message.message_id for message in memory.memory] != retained_ids:
            self.fail("モデル送信用の切り詰めによって内部保持履歴が変更されました")

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
    """判定・生成APIの呼び出し回数と設定を記録します。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        """最終回答を返し、呼び出し引数を保存します。"""
        self.calls.append(kwargs)
        response_format = kwargs["text_format"]
        if response_format is ResponseJudgment:
            output = ResponseJudgment(response_mode=ResponseMode.TEXT)
        elif response_format is GeneratedRequiredReply:
            output = GeneratedRequiredReply(content="短い返答")
        elif response_format is GeneratedReactionResponse:
            output = GeneratedReactionResponse(reply_to_message_id=1, reaction_emoji="👍")
        else:
            output = GeneratedTextResponse(action="message", content="短い返答")
        return SimpleNamespace(output_parsed=output, output=[])


class PendingMemoryToolResponses:
    """未反映情報のFunction callと、その結果を使う最終生成を再現します。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            tool_call = SimpleNamespace(
                type="function_call",
                name=PENDING_OTHER_CHANNEL_TOOL_NAME,
                call_id="pending-call",
                arguments="{}",
                model_dump=Mock(
                    return_value={
                        "type": "function_call",
                        "name": PENDING_OTHER_CHANNEL_TOOL_NAME,
                        "call_id": "pending-call",
                        "arguments": "{}",
                    }
                ),
            )
            return SimpleNamespace(output_parsed=None, output=[tool_call])
        return SimpleNamespace(
            output_parsed=GeneratedTextResponse(action="message", content="取得後の返答"),
            output=[],
        )


class FailingResponses:
    """API障害を再現します。"""

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        """判定APIの失敗を送出します。"""
        del kwargs
        msg = "API failure"
        raise RuntimeError(msg)


class ResponsePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_judges_response_with_nano_without_tools(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="起きた"))
        )

        judgment = await pipeline.judge_response(
            resolved_member_aliases={"てすたろう": 10},
        )

        if judgment.response_mode is not ResponseMode.TEXT:
            self.fail("nanoの応答要否判定を取得できません")
        call = responses.calls[0]
        if call["model"] != "gpt-5.4-nano" or call["reasoning"] != {"effort": "none"}:
            self.fail("応答要否判定がnanoの最小推論で実行されていません")
        if "tools" in call:
            self.fail("応答要否判定へ外部ツールが渡されています")
        if call["timeout"] != RESPONSE_JUDGMENT_TIMEOUT_SECONDS:
            self.fail("応答要否判定のタイムアウトが60秒ではありません")
        if "てすたろう" not in str(call["input"]):
            self.fail("判定入力に解決済み別名がありません")
        instructions = str(call["instructions"])
        if "{bot_name}" in instructions:
            self.fail("応答要否判定プロンプトへBot名を展開できていません")
        if "あなたはBotです" not in instructions or "最も自然な反応を選んで" not in instructions:
            self.fail("応答要否判定の立場または目的を明示できていません")
        if instructions.index("# response_mode") > instructions.index("# 会話入力"):
            self.fail("response_modeの説明が会話入力の説明より後にあります")

    async def test_judgment_failure_closes_without_expensive_fallback(self) -> None:
        client = cast("AsyncOpenAI", SimpleNamespace(responses=FailingResponses()))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="起きた"))
        )

        judgment = await pipeline.judge_response(
            resolved_member_aliases={},
        )

        if judgment.response_mode is not ResponseMode.NONE:
            self.fail("nano障害時に自発反応がfail-closedになっていません")

    async def test_generates_final_text_response_with_luna(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="起きた"))
        )

        generated = await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(
                response_mode=ResponseMode.TEXT,
                resolved_member_aliases={"てすたろう": 10},
            ),
        )

        if generated.content != "短い返答":
            self.fail("Lunaで生成した回答が最終結果になっていません")
        if len(responses.calls) != 1:
            self.fail("最終回答の生成でモデルが複数回呼び出されています")
        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("最終回答がLunaで生成されていません")
        if responses.calls[0]["reasoning"] != {"effort": "medium"}:
            self.fail("通常会話の推論強度が既存のmediumから変更されています")
        if "てすたろう" not in str(responses.calls[0]["input"]):
            self.fail("高品質モデルへ会話中の解決済み別名が渡されていません")
        instructions = str(responses.calls[0]["instructions"])
        if "自身の発言として自然にテキストで応答" not in instructions:
            self.fail("text生成で専用プロンプトが使用されていません")
        if "リアクションでの反応が適切だと判定済み" in instructions or "silence" in instructions:
            self.fail("text生成へ不要なreactionまたはsilenceの選択指示が混入しています")
        if "## chat役割" not in instructions or "## assistant役割" in instructions:
            self.fail("chat生成へassistant役割の指示が混入しています")
        tools = cast("list[dict[str, object]]", responses.calls[0]["tools"])
        if any(tool.get("name") == PENDING_OTHER_CHANNEL_TOOL_NAME for tool in tools):
            self.fail("未反映情報がない応答へFunction toolを追加しています")

    async def test_fetches_pending_other_channel_messages_only_after_function_call(self) -> None:
        responses = PendingMemoryToolResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="他ではどうなった？"))
        )

        generated = await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(
                response_mode=ResponseMode.TEXT,
                pending_other_channel_index='{"channels":[{"channel_name":"別チャンネル"}]}',
                pending_other_channel_context='{"messages":[{"content":"未反映の重要情報"}]}',
            ),
        )

        if generated.content != "取得後の返答" or len(responses.calls) != FUNCTION_CALL_RESPONSE_COUNT:
            self.fail("Function call後の2段目を最終回答として使用できていません")
        first_input = str(responses.calls[0]["input"])
        second_input = str(responses.calls[1]["input"])
        if "別チャンネル" not in first_input or "未反映の重要情報" in first_input:
            self.fail("最初のcallへ索引だけを渡せていません")
        if "未反映の重要情報" not in second_input:
            self.fail("Function call後に未反映本文を渡していません")
        first_tools = cast("list[dict[str, object]]", responses.calls[0]["tools"])
        if not any(tool.get("name") == PENDING_OTHER_CHANNEL_TOOL_NAME for tool in first_tools):
            self.fail("未反映情報のFunction toolが最初のcallにありません")
        if responses.calls[1].get("tool_choice") != "none":
            self.fail("未反映情報のFunction toolを1応答で複数回呼べる状態です")

    async def test_explicit_call_is_fixed_to_trigger_reply(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        trigger_message_id = 123
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=trigger_message_id, author_id=10, author_name="発言者", content="@Bot 教えて"))
        )

        generated = await pipeline.generate_response(
            ChannelRole.ASSISTANT,
            ResponseGenerationOptions(
                response_mode=ResponseMode.TEXT,
                required_reply_to_message_id=trigger_message_id,
            ),
        )

        if generated.action is not ResponseAction.REPLY or generated.reply_to_message_id != trigger_message_id:
            self.fail("明示呼びかけへの応答がトリガー投稿へのreplyに固定されていません")
        if responses.calls[0]["text_format"] is not GeneratedRequiredReply:
            self.fail("明示呼びかけでreply以外を生成できるスキーマが使われています")
        instructions = str(responses.calls[0]["instructions"])
        if "## assistant役割" not in instructions or "## chat役割" in instructions:
            self.fail("assistant生成へchat役割の指示が混入しています")

    async def test_reaction_judgment_limits_generation_to_reaction(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="やった！"))
        )

        generated = await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(response_mode=ResponseMode.REACTION),
        )

        if generated.action is not ResponseAction.REACTION:
            self.fail("reaction判定後にテキスト応答が生成されました")
        instructions = str(responses.calls[0]["instructions"])
        if "自身の反応として自然なリアクションを選んで" not in instructions:
            self.fail("reaction生成で専用プロンプトが使用されていません")
        if "日本語のテキストで回答" in instructions or "silence" in instructions:
            self.fail("reaction生成へ不要なtextまたはsilenceの選択指示が混入しています")

    async def test_applies_system_default_profile_only_to_current_request(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(
                MessageSpec(
                    message_id=1,
                    author_id=10,
                    author_name="発言者",
                    content="@Bot option poet\n詩にしてください",
                )
            )
        )
        profile = CustomProfile(
            name="poet",
            instructions="詩的な表現を使用する。",
            model="system_default",
            request_message_id=1,
            request_content="詩にしてください",
        )

        await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(response_mode=ResponseMode.TEXT, custom_profile=profile),
        )

        call = responses.calls[0]
        if call["model"] != "gpt-5.6-luna":
            self.fail("optionのsystem_defaultがChatbotの既定モデルへ解決されていません")
        if call["reasoning"] != {"effort": "low"}:
            self.fail("option指定時の推論強度がlowになっていません")
        if "詩的な表現を使用する。" not in str(call["instructions"]):
            self.fail("カスタムプロファイルの追加指示が基本指示へ追加されていません")
        if "option poet" in str(call["input"]) or "詩にしてください" not in str(call["input"]):
            self.fail("Responses APIへ渡す最新本文からoption行を分離できていません")

    async def test_uses_explicit_profile_model_with_low_reasoning(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="依頼"))
        )

        await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(
                response_mode=ResponseMode.TEXT,
                custom_profile=CustomProfile(
                    name="jargon",
                    instructions="専門用語を使う。",
                    model="gpt-5.6-luna",
                    request_message_id=1,
                    request_content="依頼",
                ),
            ),
        )

        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("optionに保存された明示モデルが使用されていません")
        if responses.calls[0]["reasoning"] != {"effort": "low"}:
            self.fail("明示モデルのoption指定で推論強度がlowになっていません")

    async def test_profile_content_override_targets_directive_message(self) -> None:
        responses = FakeResponses()
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(
                MessageSpec(
                    message_id=1,
                    author_id=10,
                    author_name="発言者",
                    content="@Bot option poet\n詩にしてください",
                )
            )
        )
        await pipeline.add_message_to_memory(
            make_message(
                MessageSpec(
                    message_id=2,
                    author_id=20,
                    author_name="別の発言者",
                    content="後から届いた通常投稿",
                )
            )
        )

        await pipeline.generate_response(
            ChannelRole.CHAT,
            ResponseGenerationOptions(
                response_mode=ResponseMode.TEXT,
                custom_profile=CustomProfile(
                    name="poet",
                    instructions="詩的な表現を使用する。",
                    model="system_default",
                    request_message_id=1,
                    request_content="詩にしてください",
                ),
            ),
        )

        api_input = str(responses.calls[0]["input"])
        if "option poet" in api_input:
            self.fail("プロファイル指定元の投稿からoption行を除去できていません")
        if "詩にしてください" not in api_input or "後から届いた通常投稿" not in api_input:
            self.fail("指定元以外の会話内容を変更しています")


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

        await MemoryDocumentUpdater(client).update({"new_messages": []})

        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("長期記憶文書の更新がLunaを使用していません")
        if responses.calls[0]["reasoning"] != {"effort": "medium"}:
            self.fail("長期記憶文書の更新の推論強度がmediumになっていません")
        if responses.calls[0]["metadata"] != {"operation": "memory_document_update"}:
            self.fail("長期記憶文書の更新種別をPlatformのmetadataへ設定していません")

    async def test_uses_separate_instructions_for_shortening_retry(self) -> None:
        responses = ConfigRecordingResponses(MemoryDocumentUpdateResult())
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))

        await MemoryDocumentUpdater(client).shorten({"documents": []})

        if responses.calls[0]["instructions"] != MEMORY_DOCUMENT_SHORTEN_INSTRUCTIONS:
            self.fail("文字数超過時に独立した短縮専用の指示を使用していません")
        if MEMORY_DOCUMENT_UPDATE_INSTRUCTIONS in str(responses.calls[0]["instructions"]):
            self.fail("短縮callへ通常更新の指示を重複送信しています")
        if responses.calls[0]["metadata"] != {"operation": "memory_document_shorten"}:
            self.fail("短縮処理の種別をPlatformのmetadataへ設定していません")
        if responses.calls[0]["text_format"] is not MemoryDocumentShortenResult:
            self.fail("短縮callが本文以外の識別情報も再生成する出力形式になっています")
