import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import discord
from discord import Message, MessageReference

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.constants import (
    ASSISTANT_DEBOUNCE_SECONDS,
    CHAT_DEBOUNCE_MAX_SECONDS,
    CHAT_DEBOUNCE_MIN_SECONDS,
    CHAT_REACTION_COOLDOWN_SECONDS,
    CHAT_TEXT_COOLDOWN_SECONDS,
)
from cogs.chatbot.models import ChannelProcessingState, CooldownStage
from cogs.chatbot.responses_api import LLMMessage, MessageInMemory, ResponseAction
from cogs.chatbot.services.history_sync import get_history_sync_after
from cogs.chatbot.services.response_policy import (
    can_change_channel_role,
    claim_response_slot,
    get_available_referenced_author_id,
    get_cooldown_stage,
    get_response_debounce_seconds,
    is_generation_current,
    should_reset_conversation,
    should_respond,
)
from cogs.chatbot.use_cases.conversation import (
    ConversationUseCases,
    get_mentioned_bot_role_ids,
)
from cogs.chatbot.use_cases.memory_query import get_latest_memory_search_query


def make_discord_message(author_id: int) -> Message:
    """返信元の発言者判定に必要な属性だけを持つDiscordメッセージを作成します。"""
    message = Mock(spec=Message)
    message.author = SimpleNamespace(id=author_id)
    return cast("Message", message)


class MemorySearchQueryTest(unittest.TestCase):
    def test_uses_only_latest_message_content_when_available(self) -> None:
        message = MessageInMemory(
            message_id=123,
            author_id=456,
            author_name="test-user",
            content="テストユーザーさんが得意なことは何でしょうか?",
            reply_to_message_id=None,
            mentioned_user_ids=[],
            timestamp=datetime.now(UTC),
        )

        result = get_latest_memory_search_query(message)

        if result != message.content:
            self.fail("最新投稿の検索クエリにメッセージIDなどのメタデータが混入しています")


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


class ShouldRespondTest(unittest.TestCase):
    def test_assistant_responds_to_mention(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=True,
            replied_to_bot=False,
        )

        if not result:
            self.fail("assistantがメンションへ応答しません")

    def test_assistant_responds_to_reply(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=True,
        )

        if not result:
            self.fail("assistantがボットへの返信へ応答しません")

    def test_assistant_ignores_regular_message(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=False,
        )

        if result:
            self.fail("assistantが明示的に呼ばれていない投稿へ応答します")

    def test_chat_always_starts_response_judgment(self) -> None:
        result = should_respond(
            ChannelRole.CHAT,
            mentioned_bot=False,
            replied_to_bot=False,
        )

        if not result:
            self.fail("chatの通常投稿で応答判断を開始しません")


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


class ChannelRolePermissionTest(unittest.TestCase):
    def test_allows_any_member_to_change_thread_role(self) -> None:
        if not can_change_channel_role(is_thread=True, manage_channels=False):
            self.fail("一般メンバーがスレッドの役割を変更できません")

    def test_requires_permission_for_regular_channel(self) -> None:
        if can_change_channel_role(is_thread=False, manage_channels=False):
            self.fail("権限のないメンバーが通常チャンネルの役割を変更できます")
        if not can_change_channel_role(is_thread=False, manage_channels=True):
            self.fail("チャンネル管理者が通常チャンネルの役割を変更できません")


class ChannelProcessingStateTest(unittest.TestCase):
    def test_different_channels_can_claim_generation_slots(self) -> None:
        first_state = ChannelProcessingState()
        second_state = ChannelProcessingState()
        first_message = make_discord_message(author_id=100)
        second_message = make_discord_message(author_id=200)

        first_claimed = claim_response_slot(
            first_state,
            first_message,
            is_explicit_call=False,
        )
        second_claimed = claim_response_slot(
            second_state,
            second_message,
            is_explicit_call=False,
        )

        if not first_claimed or not second_claimed:
            self.fail("別チャンネルの生成枠が互いに干渉しています")

    def test_same_channel_queues_latest_response_request(self) -> None:
        state = ChannelProcessingState()
        first_message = make_discord_message(author_id=100)
        second_message = make_discord_message(author_id=200)

        first_claimed = claim_response_slot(
            state,
            first_message,
            is_explicit_call=False,
        )
        second_claimed = claim_response_slot(
            state,
            second_message,
            is_explicit_call=True,
        )

        if not first_claimed:
            self.fail("最初の返信要求が生成枠を確保できません")
        if second_claimed:
            self.fail("同じチャンネルで生成枠を二重に確保しています")
        if state.queued_response_message is not second_message:
            self.fail("生成中に受けた返信要求を次回処理へ保持していません")
        if not state.queued_response_is_explicit_call:
            self.fail("生成中に受けた明示的な呼びかけを保持していません")
        if is_generation_current(state, revision=0):
            self.fail("追加の返信要求を受けても生成リビジョンが更新されていません")

    def test_channel_states_do_not_share_pending_messages(self) -> None:
        first_state = ChannelProcessingState()
        second_state = ChannelProcessingState()
        first_state.pending_messages.append(make_discord_message(author_id=100))

        if second_state.pending_messages:
            self.fail("別チャンネル間で保留メッセージを共有しています")

    def test_regular_message_does_not_replace_active_explicit_call(self) -> None:
        state = ChannelProcessingState()
        explicit_message = make_discord_message(author_id=100)
        regular_message = make_discord_message(author_id=200)
        claim_response_slot(
            state,
            explicit_message,
            is_explicit_call=True,
        )

        claim_response_slot(
            state,
            regular_message,
            is_explicit_call=False,
        )

        if state.queued_response_message is not explicit_message or not state.queued_response_is_explicit_call:
            self.fail("生成中の通常投稿によって明示呼びかけの応答必須状態が失われています")

    def test_latest_explicit_call_replaces_previous_required_call(self) -> None:
        state = ChannelProcessingState()
        first_message = make_discord_message(author_id=100)
        latest_message = make_discord_message(author_id=200)
        claim_response_slot(state, first_message, is_explicit_call=True)

        claim_response_slot(state, latest_message, is_explicit_call=True)

        if state.queued_response_message is not latest_message or not state.queued_response_is_explicit_call:
            self.fail("複数の明示呼びかけが最新の投稿へ集約されていません")


class ResponseDebounceTest(unittest.TestCase):
    def test_assistant_uses_short_fixed_delay(self) -> None:
        delay_seconds = get_response_debounce_seconds(ChannelRole.ASSISTANT)

        if delay_seconds != ASSISTANT_DEBOUNCE_SECONDS:
            self.fail("assistantの待機時間が短い固定値になっていません")

    def test_chat_delay_is_randomized_within_configured_range(self) -> None:
        delay_seconds = get_response_debounce_seconds(ChannelRole.CHAT)

        if not CHAT_DEBOUNCE_MIN_SECONDS <= delay_seconds <= CHAT_DEBOUNCE_MAX_SECONDS:
            self.fail("chatの待機時間が設定範囲を外れています")


class CooldownStageTest(unittest.TestCase):
    def test_uses_recent_stage_during_first_two_minutes(self) -> None:
        stage = get_cooldown_stage(last_action_at=999.0, now=1_000.0)

        if stage is not CooldownStage.RECENT:
            self.fail("Bot反応直後が最も厳しい判定段階になっていません")

    def test_uses_recovering_stage_between_two_and_fifteen_minutes(self) -> None:
        stage = get_cooldown_stage(last_action_at=1_000.0 - CHAT_REACTION_COOLDOWN_SECONDS, now=1_000.0)

        if stage is not CooldownStage.RECOVERING:
            self.fail("2分経過後が回復中の判定段階になっていません")

    def test_uses_ready_stage_after_fifteen_minutes(self) -> None:
        stage = get_cooldown_stage(last_action_at=1_000.0 - CHAT_TEXT_COOLDOWN_SECONDS, now=1_000.0)

        if stage is not CooldownStage.READY:
            self.fail("15分経過後に通常の判定段階へ戻りません")

    def test_uses_ready_stage_before_first_bot_action(self) -> None:
        if get_cooldown_stage(last_action_at=None, now=1_000.0) is not CooldownStage.READY:
            self.fail("Botが未反応のチャンネルでクールダウンが適用されています")


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
        cog = object.__new__(ConversationUseCases)
        channel = SimpleNamespace(id=100, send=AsyncMock())
        message = cast("Message", SimpleNamespace(channel=channel))
        state = ChannelProcessingState()
        cog.__dict__["_sent_custom_profiles"] = {}
        cog.short_term_message_repository = SimpleNamespace(set_custom_profile=AsyncMock())

        await cog.execute_response_action(
            message,
            LLMMessage(action=ResponseAction.MESSAGE, content="https://example.com"),
            state,
        )

        channel.send.assert_awaited_once_with("https://example.com", suppress_embeds=True)
        if state.last_action_at is None:
            self.fail("通常投稿の成功後にクールダウンが更新されていません")

    async def test_suppresses_embeds_for_reply(self) -> None:
        cog = object.__new__(ConversationUseCases)
        reply_to_message_id = 200
        target_message = SimpleNamespace(reply=AsyncMock())
        channel = Mock(spec=discord.TextChannel)
        channel.id = 100
        channel.get_partial_message.return_value = target_message
        message = cast("Message", SimpleNamespace(channel=channel))
        short_term_memory = SimpleNamespace(can_target_message=lambda message_id: message_id == reply_to_message_id)
        cog.response_pipelines = {100: SimpleNamespace(short_term_memory=short_term_memory)}
        cog.__dict__["_sent_custom_profiles"] = {}
        cog.short_term_message_repository = SimpleNamespace(set_custom_profile=AsyncMock())
        state = ChannelProcessingState()

        await cog.execute_response_action(
            message,
            LLMMessage(
                action=ResponseAction.REPLY,
                content="https://example.com",
                reply_to_message_id=reply_to_message_id,
            ),
            state,
        )

        target_message.reply.assert_awaited_once_with("https://example.com", suppress_embeds=True)
        if state.last_action_at is None:
            self.fail("明示replyの成功後にクールダウンが更新されていません")


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
