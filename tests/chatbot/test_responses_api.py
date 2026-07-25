import datetime
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import discord
from discord import Message

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.models import (
    CooldownStage,
    CustomProfile,
    ResponseJudgment,
    ResponseMode,
)
from cogs.chatbot.responses_api import (
    RESPONSE_JUDGMENT_TIMEOUT_SECONDS,
    AttachmentInMemory,
    GeneratedReactionResponse,
    GeneratedRequiredReply,
    GeneratedTextResponse,
    LLMMessage,
    LongTermMemoryExtractor,
    LongTermMemoryReconciler,
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
        channel=SimpleNamespace(id=100),
        guild=SimpleNamespace(id=200),
    )
    return cast("Message", message)


class ShortTermMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_marks_forwarded_content_as_unknown_third_party_statement(self) -> None:
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

        stored_content = memory.memory[0].content

        if "[転送された第三者の発言]" not in stored_content:
            self.fail("転送メッセージが第三者の発言として明示されていません")
        if "転送者による発言ではありません" not in stored_content:
            self.fail("転送本文を転送者自身の発言ではないと明示できていません")
        if "原発言者は不明" not in stored_content or "本文: 原文です" not in stored_content:
            self.fail("取得不能な原発言者と転送本文を正しく表現できていません")

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

        response_context = json.loads(memory.to_json())[0]
        memory_context = memory.memory[0].to_dict(include_reactions=False)

        if response_context["reactions"][0]["reactors"][0]["user_id"] != reactor_user_id:
            self.fail("発言生成用の文脈にリアクションした人物が含まれていません")
        if not response_context["reactions"][0]["reactors_incomplete"]:
            self.fail("リアクション利用者の不完全取得状態が保持されていません")
        if "reactions" in memory_context:
            self.fail("長期記憶用に除外したリアクションが残っています")


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
        return SimpleNamespace(output_parsed=output)


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
            ChannelRole.CHAT,
            cooldown_stage=CooldownStage.RECOVERING,
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
        if "てすたろう" not in str(call["input"]) or "recovering" not in str(call["input"]):
            self.fail("判定入力に解決済み別名またはクールダウン段階がありません")

    async def test_judgment_failure_closes_without_expensive_fallback(self) -> None:
        client = cast("AsyncOpenAI", SimpleNamespace(responses=FailingResponses()))
        pipeline = ResponsePipeline(client, "Bot")
        await pipeline.add_message_to_memory(
            make_message(MessageSpec(message_id=1, author_id=10, author_name="発言者", content="起きた"))
        )

        judgment = await pipeline.judge_response(
            ChannelRole.CHAT,
            cooldown_stage=CooldownStage.READY,
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
        if "テキストでの応答が必要だと判定済み" not in instructions:
            self.fail("text生成で専用プロンプトが使用されていません")
        if "リアクションでの反応が適切だと判定済み" in instructions or "silence" in instructions:
            self.fail("text生成へ不要なreactionまたはsilenceの選択指示が混入しています")

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
        if "リアクションでの反応が適切だと判定済み" not in instructions:
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
    async def test_extracts_memories_with_luna_without_reasoning(self) -> None:
        responses = ConfigRecordingResponses(SimpleNamespace(candidates=[]))
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))

        await LongTermMemoryExtractor(client).extract([], [])

        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("記憶抽出がLunaを使用していません")
        if responses.calls[0]["reasoning"] != {"effort": "none"}:
            self.fail("記憶抽出の推論強度がnoneになっていません")

    async def test_excludes_reactions_from_memory_extraction(self) -> None:
        responses = ConfigRecordingResponses(SimpleNamespace(candidates=[]))
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))
        message = MessageInMemory(
            message_id=1,
            author_id=10,
            author_name="発言者",
            content="本文",
            reply_to_message_id=None,
            mentioned_user_ids=[],
            timestamp=datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC),
            reactions=[
                ReactionInMemory(
                    emoji_name="👍",
                    emoji_id=None,
                    animated=False,
                    reaction_type="normal",
                    count=1,
                )
            ],
        )

        await LongTermMemoryExtractor(client).extract([message], [])

        serialized_input = json.loads(str(responses.calls[0]["input"]))
        if "reactions" in serialized_input["messages"][0]:
            self.fail("リアクションが長期記憶抽出へ渡されています")

    async def test_reconciles_memories_with_luna_without_reasoning(self) -> None:
        responses = ConfigRecordingResponses(SimpleNamespace(action="keep", existing_memory_ids=[]))
        client = cast("AsyncOpenAI", SimpleNamespace(responses=responses))

        await LongTermMemoryReconciler(client).reconcile({}, [{}], correction_only=False)

        if responses.calls[0]["model"] != "gpt-5.6-luna":
            self.fail("記憶の訂正・競合判定がLunaを使用していません")
        if responses.calls[0]["reasoning"] != {"effort": "none"}:
            self.fail("記憶の訂正・競合判定の推論強度がnoneになっていません")
