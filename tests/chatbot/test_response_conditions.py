import asyncio
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, AsyncMock, Mock

import discord
from discord import Message, MessageReference

from cogs.chatbot.services.history_sync import get_history_sync_after
from cogs.chatbot.services.message_delivery import reply_with_split_response, send_split_response
from cogs.chatbot.services.response_policy import (
    get_available_referenced_author_id,
    should_reset_conversation,
)
from cogs.chatbot.use_cases.conversation import (
    ConversationUseCases,
    get_mentioned_bot_role_ids,
)


def make_discord_message(author_id: int) -> Message:
    """返信元の発言者判定に必要な属性だけを持つDiscordメッセージを作成します。"""
    message = Mock(spec=Message)
    message.author = SimpleNamespace(id=author_id)
    return cast("Message", message)


class HistorySyncRangeTest(unittest.TestCase):
    def test_uses_latest_stored_message_for_existing_channel(self) -> None:
        now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
        latest_stored_at = now - timedelta(hours=2)

        if get_history_sync_after(latest_stored_at, now) != latest_stored_at:
            self.fail("保存済みチャンネルの差分取得が最新投稿の後から始まりません")

    def test_uses_twelve_hours_for_channel_without_stored_messages(self) -> None:
        now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

        if get_history_sync_after(None, now) != now - timedelta(hours=12):
            self.fail("未保存チャンネルの履歴取得範囲が12時間になっていません")

    def test_limits_existing_channel_history_to_thirty_days(self) -> None:
        now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

        if get_history_sync_after(now - timedelta(days=60), now) != now - timedelta(days=30):
            self.fail("保存済みチャンネルの履歴取得が30日を超えています")


class StartupHistorySyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_long_term_memory_before_releasing_message_events(self) -> None:
        events: list[str] = []
        use_cases = object.__new__(ConversationUseCases)
        use_cases.bot = SimpleNamespace(user=SimpleNamespace(id=123))
        use_cases.reply_conversations = SimpleNamespace(repository=SimpleNamespace(namespace=""))
        use_cases.runtime_environment = SimpleNamespace(is_debug=False)
        use_cases.__dict__["_history_sync_complete"] = asyncio.Event()
        use_cases.__dict__["_history_sync_lock"] = asyncio.Lock()

        async def synchronize() -> None:
            events.append("history")

        async def initialize_memory() -> None:
            if use_cases._history_sync_complete.is_set():  # noqa: SLF001
                self.fail("長期記憶の初期化前に通常メッセージ処理を解放しています")
            events.append("memory")

        use_cases._synchronize_recent_discord_history = AsyncMock(side_effect=synchronize)  # type: ignore[method-assign]  # noqa: SLF001
        use_cases._initialize_long_term_memory_if_enabled = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
            side_effect=initialize_memory
        )

        await use_cases.on_ready()

        if events != ["history", "memory"] or not use_cases._history_sync_complete.is_set():  # noqa: SLF001
            self.fail("履歴同期、長期記憶初期化、通常処理解放の順序が正しくありません")

    async def test_history_sync_only_saves_messages_until_all_channels_are_complete(self) -> None:
        synchronized_message = SimpleNamespace(id=10)

        async def history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
            yield synchronized_message

        channel = SimpleNamespace(
            id=20,
            permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, read_message_history=True)),
            history=history,
        )
        guild = SimpleNamespace(me=SimpleNamespace(), text_channels=[channel], threads=[])
        use_cases = object.__new__(ConversationUseCases)
        use_cases.bot = SimpleNamespace(guilds=[guild])
        use_cases.runtime_environment = SimpleNamespace(
            should_process_chatbot_channel=Mock(return_value=True),
        )
        use_cases.short_term_message_repository = SimpleNamespace(
            get_latest_created_at=AsyncMock(return_value=None),
        )
        use_cases._ensure_channel_state = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]  # noqa: SLF001
        use_cases._refresh_retained_message_reactions = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
        use_cases._append_message_to_short_term_memory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
        use_cases._enqueue_long_term_memory = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

        await use_cases._synchronize_recent_discord_history()  # noqa: SLF001

        use_cases._append_message_to_short_term_memory.assert_awaited_once_with(  # noqa: SLF001
            synchronized_message,
            ANY,
        )
        use_cases._enqueue_long_term_memory.assert_not_awaited()  # noqa: SLF001


class BotRoleMentionTest(unittest.TestCase):
    def test_detects_mentioned_same_name_role_assigned_to_bot(self) -> None:
        same_name_role = SimpleNamespace(id=10, name="Papyrus")
        message = SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(roles=[same_name_role])),
            role_mentions=[same_name_role],
        )
        bot_user = SimpleNamespace(id=1, name="Papyrus")

        result = get_mentioned_bot_role_ids(cast("Message", message), cast("discord.ClientUser", bot_user))

        if result != {10}:
            self.fail("Botに付与された同名ロールへのメンションを検出できません")

    def test_ignores_unassigned_role_with_same_name(self) -> None:
        assigned_role = SimpleNamespace(id=10, name="Papyrus")
        mentioned_role = SimpleNamespace(id=20, name="Papyrus")
        message = SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(roles=[assigned_role])),
            role_mentions=[mentioned_role],
        )
        bot_user = SimpleNamespace(id=1, name="Papyrus")

        result = get_mentioned_bot_role_ids(cast("Message", message), cast("discord.ClientUser", bot_user))

        if result:
            self.fail("Botに付与されていない同名ロールへのメンションを誤検出しています")

    def test_ignores_assigned_role_with_different_name(self) -> None:
        role = SimpleNamespace(id=10, name="Another Role")
        message = SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(roles=[role])),
            role_mentions=[role],
        )
        bot_user = SimpleNamespace(id=1, name="Papyrus")

        result = get_mentioned_bot_role_ids(cast("Message", message), cast("discord.ClientUser", bot_user))

        if result:
            self.fail("Bot名と異なる付与ロールへのメンションを誤検出しています")


class ConversationResetTest(unittest.TestCase):
    def test_resets_after_configured_interval_from_last_human_message(self) -> None:
        last_human_message_timestamp = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)

        should_reset = should_reset_conversation(
            last_human_message_timestamp,
            last_human_message_timestamp + timedelta(minutes=720),
            reset_minutes=720,
        )

        if not should_reset:
            self.fail("設定時間が経過しても会話をリセットしません")

    def test_does_not_reset_without_a_previous_human_message(self) -> None:
        should_reset = should_reset_conversation(
            None,
            datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
            reset_minutes=720,
        )

        if should_reset:
            self.fail("過去の人間投稿がないチャンネルで会話をリセットします")


class ReferencedAuthorTest(unittest.TestCase):
    def test_uses_resolved_message_before_cached_message(self) -> None:
        resolved_author_id = 100
        reference = cast(
            "MessageReference",
            SimpleNamespace(
                resolved=make_discord_message(resolved_author_id),
                cached_message=make_discord_message(200),
            ),
        )

        author_id = get_available_referenced_author_id(reference)

        if author_id != resolved_author_id:
            self.fail("Discordイベントに同梱された返信元メッセージを優先していません")

    def test_falls_back_to_cached_message(self) -> None:
        cached_author_id = 200
        reference = cast(
            "MessageReference",
            SimpleNamespace(resolved=None, cached_message=make_discord_message(cached_author_id)),
        )

        author_id = get_available_referenced_author_id(reference)

        if author_id != cached_author_id:
            self.fail("返信元メッセージのキャッシュを利用できません")

    def test_returns_none_when_reference_has_no_message(self) -> None:
        reference = cast("MessageReference", SimpleNamespace(resolved=None, cached_message=None))

        author_id = get_available_referenced_author_id(reference)

        if author_id is not None:
            self.fail("返信元メッセージがない状態で発言者IDを返しています")


class EmbedSuppressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_suppresses_embeds_for_channel_message(self) -> None:
        """通常送信でリンクの展開を抑止します。"""
        channel = Mock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        await send_split_response(channel, "https://example.com")
        channel.send.assert_awaited_once_with("https://example.com", suppress_embeds=True)

    async def test_suppresses_embeds_for_reply(self) -> None:
        """返信送信でリンクの展開を抑止します。"""
        message = Mock(spec=Message)
        message.reply = AsyncMock()
        await reply_with_split_response(message, "https://example.com")
        message.reply.assert_awaited_once_with("https://example.com", suppress_embeds=True)


class LongTermMemoryExclusionFlagTest(unittest.IsolatedAsyncioTestCase):
    async def test_records_exclusion_even_if_message_event_has_not_been_saved(self) -> None:
        message_id = 123
        cog = object.__new__(ConversationUseCases)
        cog.__dict__["_long_term_memory_excluded_message_ids"] = set()
        repository = SimpleNamespace(exclude_from_long_term_memory=AsyncMock())
        cog.short_term_message_repository = repository
        message = cast("Message", SimpleNamespace(id=message_id))

        await cog.exclude_from_long_term_memory(message)

        repository.exclude_from_long_term_memory.assert_awaited_once_with(message_id)
        if message_id not in cog.__dict__["_long_term_memory_excluded_message_ids"]:
            self.fail("保存イベントとの競合を吸収する除外フラグが保持されていません")
