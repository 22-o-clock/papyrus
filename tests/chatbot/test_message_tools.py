# ruff: noqa: PT009, PT027 -- unittestで実行するため標準のアサーションを使用。
import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord

from cogs.chatbot.models.reply_conversation import ConversationMessage, ReplyConversation
from cogs.chatbot.services.conversation_agent import build_agent_tools
from cogs.chatbot.services.conversation_tools import ConversationTools


def post(number: int) -> SimpleNamespace:
    """投稿の取得順序を検証できるテストデータを返します。"""
    return SimpleNamespace(id=number, jump_url=f"https://discord.com/channels/1/10/{number}")


def history(**kwargs: Any) -> Any:  # noqa: ANN401
    """要求した側の投稿をDiscordと同じ順序で返します。"""
    iterator = AsyncMock()
    iterator.__aiter__.return_value = [post(9), post(8)] if "before" in kwargs else [post(11), post(12)]
    return iterator


class MessageToolsTest(unittest.IsolatedAsyncioTestCase):
    """検索・直接取得・前後取得の結果と権限境界を検証します。"""

    def setUp(self) -> None:
        self.request = AsyncMock()
        guild = SimpleNamespace(id=1, me=None, _state=SimpleNamespace(http=SimpleNamespace(request=self.request)))
        self.tools: Any = ConversationTools(cast("Any", guild), cast("Any", None), cast("Any", None), 20, cast("Any", None))
        self.channel = SimpleNamespace(id=10, fetch_message=AsyncMock(side_effect=post), history=Mock(side_effect=history))
        self.tools.channel = AsyncMock(return_value=self.channel)

        async def snapshot(value: SimpleNamespace) -> ConversationMessage:
            return ConversationMessage(
                message_id=value.id,
                channel_id=10,
                author_id=30,
                author_name="person",
                created_at="2026-01-01T00:00:00Z",
                content=f"text {value.id}",
            )

        self.tools.snapshot = AsyncMock(side_effect=snapshot)

    async def execute(self, name: str, **args: Any) -> Any:  # noqa: ANN401
        """登録されたツールをJSON経由で呼び出します。"""
        return json.loads(str(await self.tools.execute(name, json.dumps(args))))

    async def test_context_orders_both_sides_and_anchor(self) -> None:
        result = await self.execute("get_message_context", channel_id="10", message_id="10", before_limit=2, after_limit=2)
        self.assertEqual([m["message_id"] for m in result["messages"]], [8, 9, 10, 11, 12])
        self.assertEqual(result["anchor_message_id"], "10")
        calls = self.channel.history.call_args_list
        self.assertFalse(calls[0].kwargs["oldest_first"])
        self.assertTrue(calls[1].kwargs["oldest_first"])

    async def test_zero_context_only_returns_anchor(self) -> None:
        result = await self.execute("get_message_context", channel_id="10", message_id="10", before_limit=0, after_limit=0)
        self.assertEqual([m["message_id"] for m in result["messages"]], [10])
        self.channel.history.assert_not_called()

    async def test_direct_message_contains_source_link(self) -> None:
        result = await self.execute("get_message", channel_id="10", message_id="42")
        self.assertEqual(result["content"], "text 42")
        self.assertEqual(result["url"], "https://discord.com/channels/1/10/42")

    async def test_permission_failure_prevents_fetch(self) -> None:
        self.tools.channel.side_effect = ValueError("forbidden")
        with self.assertRaises(ValueError):
            await self.execute("get_message", channel_id="11", message_id="42")
        self.channel.fetch_message.assert_not_awaited()

    async def test_search_filters_and_sparse_page_continuation(self) -> None:
        self.request.return_value = {"messages": [[{"id": "42", "channel_id": "10", "hit": True}]], "total_results": 80}
        result = await self.execute(
            "search_messages",
            query="旅行",
            channel_id="10",
            author_id="30",
            after="2020-01-01T00:00:00+09:00",
            before="2021-01-01T00:00:00+09:00",
            limit=25,
            offset=25,
        )
        self.assertEqual(result["next_offset"], 50)
        self.assertEqual(result["messages"][0]["content"], "text 42")
        params = self.request.call_args.kwargs["params"]
        self.assertEqual(params["content"], "旅行")
        self.assertEqual(params["author_id"], ["30"])
        self.assertEqual(params["channel_id"], ["10"])
        self.assertLess(int(params["min_id"]), int(params["max_id"]))

    async def test_search_default_has_no_date_or_channel_restriction(self) -> None:
        self.request.return_value = {"messages": [], "total_results": 0}
        result = await self.execute("search_messages")
        self.assertEqual(
            self.request.call_args.kwargs["params"], {"limit": 25, "offset": 0, "sort_by": "timestamp", "sort_order": "desc"}
        )
        self.assertIsNone(result["next_offset"])

    async def test_indexing_is_distinct_from_empty_results(self) -> None:
        self.request.return_value = {"code": 110000, "retry_after": 2}
        result = await self.execute("search_messages", query="test")
        self.assertEqual(result, {"status": "indexing", "retry_after": 2})

    async def test_search_omits_context_duplicates_and_inaccessible_posts(self) -> None:
        self.request.return_value = {
            "messages": [
                [{"id": "41", "channel_id": "10", "hit": False}, {"id": "42", "channel_id": "10"}],
                [{"id": "42", "channel_id": "10"}, {"id": "43", "channel_id": "11"}],
            ],
            "total_results": 2,
        }
        self.tools.channel.side_effect = [self.channel, ValueError("forbidden")]
        result = await self.execute("search_messages", query="test")
        self.assertEqual([m["message_id"] for m in result["messages"]], [42])
        self.assertEqual(result["unavailable_message_ids"], ["43"])
        self.channel.fetch_message.assert_awaited_once_with(42)

    async def test_pagination_limit_is_explicit(self) -> None:
        self.request.return_value = {"messages": [], "total_results": 11000}
        result = await self.execute("search_messages", offset=9975)
        self.assertTrue(result["pagination_limited"])
        self.assertIsNone(result["next_offset"])

    async def test_invalid_filters_do_not_call_discord(self) -> None:
        for args in (
            {"limit": True},
            {"limit": 26},
            {"offset": -1},
            {"query": "a" * 1025},
            {"after": "2020-01-01"},
            {"after": "2021-01-01T00:00:00Z", "before": "2020-01-01T00:00:00Z"},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.execute("search_messages", **args)
        self.request.assert_not_awaited()

    async def test_actual_channel_guard_rejects_other_guild_and_hidden_history(self) -> None:
        channel = Mock(spec=discord.TextChannel)
        guild = SimpleNamespace(id=1, me=SimpleNamespace(display_name="bot"), get_channel_or_thread=Mock(return_value=channel))
        tools = ConversationTools(cast("Any", guild), cast("Any", None), cast("Any", None), 20, cast("Any", None))
        for guild_id, view, read in ((2, True, True), (1, False, True), (1, True, False)):
            channel.guild.id = guild_id
            channel.permissions_for.return_value = SimpleNamespace(view_channel=view, read_message_history=read)
            with self.subTest(guild_id=guild_id, view=view, read=read), self.assertRaises(ValueError):
                await tools.channel(10)

    def test_new_tools_are_available_to_agent(self) -> None:
        names = {tool.name for tool in build_agent_tools(self.tools, ReplyConversation(), 1)}
        self.assertTrue({"search_messages", "get_message", "get_message_context"} <= names)
