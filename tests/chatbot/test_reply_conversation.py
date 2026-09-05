import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx2
from agents import FunctionTool
from openai import NotFoundError
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from cogs.chatbot.models.reply_conversation import ConversationAttachment, ConversationMessage, ReplyConversation
from cogs.chatbot.services.conversation_agent import build_agent_tools
from cogs.chatbot.services.conversation_tools import ConversationTools
from cogs.chatbot.use_cases.reply_conversation import ReplyConversationUseCases


def ensure(condition: object) -> None:
    if not condition:
        raise AssertionError


def answer() -> Response:
    """実際のSDKが処理できる回答オブジェクトを作ります。"""
    return Response.model_construct(
        id="resp_new",
        usage=None,
        output=[
            ResponseOutputMessage(
                id="msg_answer",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="answer", annotations=[])],
            )
        ],
    )


def message(number: int, parent: int | None = None, *, assistant: bool = False) -> ConversationMessage:
    return ConversationMessage(
        message_id=number,
        channel_id=10,
        author_id=20 if assistant else 30,
        author_name="bot" if assistant else "person",
        created_at="2026-09-05T00:00:00Z",
        content=f"message {number}",
        parent_id=parent,
        is_assistant=assistant,
    )


class MemoryRepository:
    def __init__(self) -> None:
        self.messages: dict[int, ConversationMessage] = {}
        self.turns: dict[int, ReplyConversation] = {}

    async def get_message(self, number: int) -> ConversationMessage | None:
        value = self.messages.get(number)
        return value.model_copy(deep=True) if value else None

    async def save_message(self, value: ConversationMessage) -> ConversationMessage:
        self.messages.setdefault(value.message_id, value.model_copy(deep=True))
        return self.messages[value.message_id].model_copy(deep=True)

    async def get_turn(self, number: int) -> ReplyConversation | None:
        value = self.turns.get(number)
        return value.model_copy(deep=True) if value else None

    async def save_turn(self, number: int, value: ReplyConversation) -> None:
        self.turns[number] = value.model_copy(deep=True)


class ReplyConversationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.responses = SimpleNamespace(
            create=AsyncMock(),
            input_tokens=SimpleNamespace(count=AsyncMock(return_value=SimpleNamespace(input_tokens=100))),
        )
        self.client = SimpleNamespace(responses=self.responses)
        self.engine = ReplyConversationUseCases(cast("Any", self.client), cast("Any", self.repository))  # type: ignore[arg-type]
        self.typing = AsyncMock()
        self.incoming = SimpleNamespace(
            id=3, channel=SimpleNamespace(id=10, name="general", typing=Mock(return_value=self.typing))
        )
        self.tools: Any = SimpleNamespace(
            snapshot=AsyncMock(),
            channel=AsyncMock(),
            bot_name="Papyrus",
            messages=SimpleNamespace(get_attachments=AsyncMock(return_value=[])),
            execute=AsyncMock(return_value="PRIVATE TOOL RESULT"),
        )

    async def test_member_list_attaches_aliases_only_to_current_members(self) -> None:
        """有効な別名を本人に結び付け、未登録者は空配列、サーバー外の人物は除外します。"""
        members = [
            SimpleNamespace(id=30, name="taro", display_name="太郎", bot=False),
            SimpleNamespace(id=40, name="hanako", display_name="花子", bot=False),
        ]
        member_iterator = AsyncMock()
        member_iterator.__aiter__.return_value = members
        guild = SimpleNamespace(me=None, fetch_members=Mock(return_value=member_iterator))
        aliases = SimpleNamespace(get_active_aliases=AsyncMock(return_value={"たろ": 30, "たー": 30, "別の人": 99}))
        tools = ConversationTools(cast("Any", guild), cast("Any", None), cast("Any", None), 20, cast("Any", aliases))
        result = json.loads(str(await tools.execute("list_members", "{}")))
        ensure(
            result
            == [
                {"user_id": "30", "name": "taro", "display_name": "太郎", "bot": False, "aliases": ["たろ", "たー"]},
                {"user_id": "40", "name": "hanako", "display_name": "花子", "bot": False, "aliases": []},
            ]
        )
        aliases.get_active_aliases.assert_awaited_once()
        guild.fetch_members.assert_called_once_with(limit=None)

    async def test_channel_list_nests_categories_channels_and_threads(self) -> None:
        """所属なし・空カテゴリ・アーカイブ・非表示チャンネルを含む階層を検証します。"""
        category = Mock(spec=discord.CategoryChannel, id=1, position=0)
        category.name = "交流"
        empty_category = Mock(spec=discord.CategoryChannel, id=2, position=1)
        empty_category.name = "空カテゴリ"
        channel = Mock(spec=discord.TextChannel, id=10, category_id=1, position=0, type=discord.ChannelType.text)
        channel.name = "雑談"
        uncategorized = Mock(spec=discord.VoiceChannel, id=20, category_id=None, position=1, type=discord.ChannelType.voice)
        uncategorized.name = "通話"
        hidden = Mock(spec=discord.TextChannel, id=30)
        hidden.permissions_for.return_value.view_channel = False
        active = SimpleNamespace(
            id=100, name="進行中", parent_id=10, archived=False, permissions_for=lambda _: SimpleNamespace(view_channel=True)
        )
        archived = SimpleNamespace(id=101, name="終了", parent_id=10, archived=True)
        iterator = AsyncMock()
        iterator.__aiter__.return_value = [archived]
        channel.archived_threads.return_value = iterator
        guild = SimpleNamespace(
            me=SimpleNamespace(display_name="Bot"),
            fetch_channels=AsyncMock(return_value=[channel, category, empty_category, uncategorized, hidden]),
            active_threads=AsyncMock(return_value=[active]),
        )
        tools = ConversationTools(cast("Any", guild), cast("Any", None), cast("Any", None), 99, cast("Any", None))
        result = json.loads(str(await tools.execute("list_channels", "{}")))
        ensure(
            result
            == {
                "categories": [
                    {
                        "category_id": "1",
                        "name": "交流",
                        "channels": [
                            {
                                "channel_id": "10",
                                "name": "雑談",
                                "type": "text",
                                "threads": [
                                    {"channel_id": "100", "name": "進行中", "archived": False},
                                    {"channel_id": "101", "name": "終了", "archived": True},
                                ],
                            },
                        ],
                    },
                    {"category_id": "2", "name": "空カテゴリ", "channels": []},
                    {
                        "category_id": None,
                        "name": None,
                        "channels": [
                            {"channel_id": "20", "name": "通話", "type": "voice", "threads": []},
                        ],
                    },
                ],
                "incomplete_archive_channels": [],
            }
        )

    async def test_typing_stops_before_sending_and_persisting_reply(self) -> None:
        events = []
        self.typing.__aenter__.side_effect = lambda: events.append("typing_start")
        self.typing.__aexit__.side_effect = lambda *_: events.append("typing_stop")
        self.repository.messages[3] = message(3)
        self.tools.snapshot.return_value = message(4, 3, assistant=True)

        async def generate(**_: object) -> Response:
            events.append("generate")
            return answer()

        async def send(*_: object) -> list[SimpleNamespace]:
            events.append("send")
            return [SimpleNamespace(id=4)]

        self.responses.create.side_effect = generate
        with patch("cogs.chatbot.use_cases.reply_conversation.reply_with_split_response", new=send):
            await self.engine.respond(self.incoming, self.tools, None)
        ensure(events == ["typing_start", "generate", "typing_stop", "send"])
        ensure(self.repository.turns[4].response_id == "resp_new")

    def test_user_input_separates_body_metadata_and_media(self) -> None:
        """本文を先頭に置き、メタデータから本文とチャンネルIDを除外することを確認します。"""
        incoming = message(3, 2)
        incoming.attachments = [
            ConversationAttachment(attachment_id=5, filename="photo.png", url="https://example.com/photo.png", kind="image")
        ]
        parts = incoming.as_input(include_media=True)["content"]
        ensure(parts[0] == {"type": "input_text", "text": "message 3"})
        metadata = json.loads(parts[1]["text"])["metadata"]
        ensure("content" not in metadata)
        ensure("channel_id" not in metadata)
        ensure(metadata["author_id"] == incoming.author_id)
        ensure(metadata["parent_id"] == incoming.parent_id)
        ensure(metadata["attachments"][0]["attachment_id"] == incoming.attachments[0].attachment_id)
        ensure("url" not in metadata["attachments"][0])
        ensure(parts[2] == {"type": "input_image", "image_url": "https://example.com/photo.png"})
        ensure(incoming.as_input(include_media=False)["content"] == parts[:2])

    async def test_typing_stops_when_generation_fails(self) -> None:
        self.repository.messages[3] = message(3)
        self.responses.create.side_effect = ValueError("generation failed")
        try:
            await self.engine.respond(self.incoming, self.tools, None)
        except ValueError:
            pass
        else:
            self.fail("Generation should fail")
        self.typing.__aexit__.assert_awaited_once()

    async def test_follows_only_ancestors_and_preserves_original_text(self) -> None:
        self.repository.messages = {1: message(1), 2: message(2, 1), 3: message(3, 1)}
        self.tools.snapshot.return_value = message(4, 2)
        state, pending = await self.engine.load(SimpleNamespace(id=4), self.tools)  # type: ignore[arg-type]
        ensure([m.message_id for m in state.messages] == [1, 2, 4])
        ensure([m.message_id for m in pending] == [1, 2, 4])
        self.tools.snapshot.return_value.content = "edited"
        state, _ = await self.engine.load(SimpleNamespace(id=4), self.tools)  # type: ignore[arg-type]
        ensure(state.messages[-1].content == "message 4")

    async def test_resume_after_restart_and_sibling_branches_are_independent(self) -> None:
        self.repository.messages[2] = message(2, 1, assistant=True)
        self.repository.turns[2] = ReplyConversation(
            response_id="resp_old",
            messages=[message(1), message(2, 1, assistant=True)],
            profile={"name": "test", "model": "gpt-5.6-terra", "instructions": "test"},
        )
        self.tools.snapshot.return_value = message(3, 2)
        state, pending = await self.engine.load(SimpleNamespace(id=3), self.tools)  # type: ignore[arg-type]
        ensure(state.response_id == "resp_old")
        ensure(len(pending) == 1)
        ensure((state.profile or {})["name"] == "test")  # type: ignore[index]
        self.tools.snapshot.return_value = message(4, 2)
        sibling, _ = await self.engine.load(SimpleNamespace(id=4), self.tools)  # type: ignore[arg-type]
        ensure([m.message_id for m in sibling.messages] == [1, 2, 4])

    async def test_missing_parent_keeps_available_history(self) -> None:
        self.tools.snapshot.return_value = message(2, 1)
        self.tools.channel.side_effect = ValueError("missing")
        state, _ = await self.engine.load(SimpleNamespace(id=2), self.tools)  # type: ignore[arg-type]
        ensure(state.missing_history)
        ensure(len(state.messages) == 1)

    async def test_rebuild_keeps_current_media_and_does_not_summarize_if_it_fits(self) -> None:
        old, current = message(1), message(2, 1)
        for value in [old, current]:
            value.attachments = [
                ConversationAttachment(
                    attachment_id=value.message_id,
                    filename="image.png",
                    kind="image",
                    url=f"https://example.com/{value.message_id}",
                    summary="添付の要約",
                )
            ]
        state = ReplyConversation(response_id="resp_old", messages=[old, current], calls=[{"name": "get_memory"}])
        inputs = await self.engine.rebuild(state, 2, self.tools)
        serialized = json.dumps(inputs, ensure_ascii=False)
        ensure("https://example.com/1" not in serialized)
        ensure("https://example.com/2" in serialized)
        ensure("添付の要約" in serialized)
        ensure("get_memory" in serialized)
        ensure(state.response_id is None)
        self.responses.create.assert_not_awaited()

    async def test_rebuild_retains_text_without_local_token_counting(self) -> None:
        """復元時は本文を削らず、圧縮をサーバーに任せます。"""
        state = ReplyConversation(messages=[message(1), message(2, 1)])
        inputs = await self.engine.rebuild(state, 2, self.tools)
        ensure([m.message_id for m in state.messages] == [1, 2])
        ensure(inputs == state.inputs(2, compact=True))
        self.responses.input_tokens.count.assert_not_awaited()
        self.responses.create.assert_not_awaited()

    async def test_tool_results_are_returned_but_only_call_records_are_persisted(self) -> None:
        state = ReplyConversation()
        tool = next(
            t for t in build_agent_tools(self.tools, state, 1) if isinstance(t, FunctionTool) and t.name == "get_memory"
        )
        output = await tool.on_invoke_tool(cast("Any", None), '{"kind":"shared","user_id":null}')
        ensure(output == "PRIVATE TOOL RESULT")
        ensure("PRIVATE TOOL RESULT" not in state.model_dump_json())
        ensure(state.calls[0]["name"] == "get_memory")

    async def test_continuation_sends_only_new_message_and_inherits_profile(self) -> None:
        self.repository.messages[2] = message(2, 1, assistant=True)
        self.repository.turns[2] = ReplyConversation(
            response_id="resp_old",
            messages=[message(1), message(2, 1, assistant=True)],
            profile={"name": "test", "model": "gpt-5.6-terra", "instructions": "custom"},
        )
        self.tools.snapshot.side_effect = [message(3, 2), message(4, 3, assistant=True)]
        self.responses.create.return_value = answer()
        with patch(
            "cogs.chatbot.use_cases.reply_conversation.reply_with_split_response",
            new=AsyncMock(return_value=[SimpleNamespace(id=4)]),
        ):
            await self.engine.respond(self.incoming, self.tools, None)  # type: ignore[arg-type]
        params = self.responses.create.call_args.kwargs
        ensure(params["previous_response_id"] == "resp_old")
        ensure(params["model"] == "gpt-5.6-terra")
        ensure(params["context_management"] == [{"type": "compaction", "compact_threshold": 30_000}])
        self.responses.input_tokens.count.assert_not_awaited()
        ensure(len(params["input"]) == 1)
        ensure("custom" in params["instructions"])
        ensure("#general (id: 10)" in params["instructions"])
        for name in ("get_messages", "list_channels", "get_attachment", "list_members", "get_memory"):
            ensure(f"{name}()" in params["instructions"])
        ensure("{function_name_" not in params["instructions"])
        ensure("{channel_" not in params["instructions"])
        ensure(self.repository.turns[4].response_id == "resp_new")

    async def test_expired_response_is_rebuilt_from_saved_conversation(self) -> None:
        self.repository.messages[2] = message(2, 1, assistant=True)
        self.repository.turns[2] = ReplyConversation(
            response_id="resp_expired",
            messages=[message(1), message(2, 1, assistant=True)],
        )
        error = NotFoundError(
            "Response not found",
            response=httpx2.Response(404, request=httpx2.Request("POST", "https://api.openai.com/v1/responses")),
            body={"param": "previous_response_id"},
        )
        self.responses.create.side_effect = [error, answer()]
        self.tools.snapshot.side_effect = [message(3, 2), message(4, 3, assistant=True)]
        self.responses.create.return_value = answer()
        with patch(
            "cogs.chatbot.use_cases.reply_conversation.reply_with_split_response",
            new=AsyncMock(return_value=[SimpleNamespace(id=4)]),
        ):
            await self.engine.respond(self.incoming, self.tools, None)
        params = self.responses.create.call_args.kwargs
        ensure(not isinstance(params.get("previous_response_id"), str))
        expected_message_count = 3
        ensure(len(params["input"]) == expected_message_count)

    async def test_removed_media_is_not_reintroduced_by_later_reconstruction(self) -> None:
        old = message(1)
        old.attachments = [
            ConversationAttachment(
                attachment_id=1, filename="old.pdf", kind="pdf", url="https://example.com/old.pdf", summary="要約"
            )
        ]
        state = ReplyConversation(messages=[old, message(2, 1)])
        await self.engine.rebuild(state, 2, self.tools)
        ensure("https://example.com/old.pdf" not in json.dumps(state.inputs(3, compact=False)))

    async def test_tool_failure_returns_error_without_losing_call_record(self) -> None:
        self.tools.execute.side_effect = ValueError("not found")
        state = ReplyConversation()
        tool = next(
            t for t in build_agent_tools(self.tools, state, 1) if isinstance(t, FunctionTool) and t.name == "get_attachment"
        )
        output = await tool.on_invoke_tool(cast("Any", None), "{}")
        ensure(json.loads(output)["error"] == "not found")
        ensure(state.calls[0]["status"] == "failed")

    async def test_sdk_executes_attachment_and_continues_with_native_media(self) -> None:
        """実際のRunnerでツール実行から画像・PDF付きの継続生成までを検証します。"""
        self.repository.messages[3] = message(3)
        self.tools.snapshot.return_value = message(4, 3, assistant=True)
        self.tools.execute.return_value = [
            {"type": "input_image", "image_url": "https://example.com/image.png"},
            {"type": "input_file", "file_url": "https://example.com/file.pdf"},
        ]
        call = ResponseFunctionToolCall(
            id="fc_1",
            type="function_call",
            call_id="call_1",
            name="get_attachment",
            arguments='{"channel_id":"10","message_id":"3","attachment_id":"5"}',
        )
        self.responses.create.side_effect = [
            Response.model_construct(id="resp_tool", usage=None, output=[call]),
            answer(),
        ]
        with patch(
            "cogs.chatbot.use_cases.reply_conversation.reply_with_split_response",
            new=AsyncMock(return_value=[SimpleNamespace(id=4)]),
        ):
            await self.engine.respond(self.incoming, self.tools, None)
        self.tools.execute.assert_awaited_once_with("get_attachment", call.arguments)
        params = self.responses.create.call_args.kwargs
        ensure(params["previous_response_id"] == "resp_tool")
        ensure(
            params["input"]
            == [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": self.tools.execute.return_value,
                }
            ]
        )
        ensure(self.repository.turns[4].response_id == "resp_new")
        ensure(self.repository.turns[4].calls[0]["name"] == "get_attachment")
        ensure("example.com" not in json.dumps(self.repository.turns[4].calls))


if __name__ == "__main__":
    unittest.main()
