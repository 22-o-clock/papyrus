import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from discord import Message, MessageReference

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.chatbot_cog import (
    ASSISTANT_DEBOUNCE_SECONDS,
    CHAT_DEBOUNCE_MAX_SECONDS,
    CHAT_DEBOUNCE_MIN_SECONDS,
    CHAT_REACTION_COOLDOWN_SECONDS,
    CHAT_TEXT_COOLDOWN_SECONDS,
    ChannelProcessingState,
    can_change_channel_role,
    can_execute_spontaneous_action,
    can_start_spontaneous_generation,
    claim_response_slot,
    get_available_referenced_author_id,
    get_response_debounce_seconds,
    is_generation_current,
    should_respond,
)
from cogs.chatbot.responses_api import ResponseAction


def make_discord_message(author_id: int) -> Message:
    """返信元の発言者判定に必要な属性だけを持つDiscordメッセージを作成します。"""
    message = Mock(spec=Message)
    message.author = SimpleNamespace(id=author_id)
    return cast("Message", message)


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

        first_claimed = claim_response_slot(first_state, first_message, is_explicit_call=False)
        second_claimed = claim_response_slot(second_state, second_message, is_explicit_call=False)

        if not first_claimed or not second_claimed:
            self.fail("別チャンネルの生成枠が互いに干渉しています")

    def test_same_channel_queues_latest_response_request(self) -> None:
        state = ChannelProcessingState()
        first_message = make_discord_message(author_id=100)
        second_message = make_discord_message(author_id=200)

        first_claimed = claim_response_slot(state, first_message, is_explicit_call=False)
        second_claimed = claim_response_slot(state, second_message, is_explicit_call=True)

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


class ResponseDebounceTest(unittest.TestCase):
    def test_assistant_uses_short_fixed_delay(self) -> None:
        delay_seconds = get_response_debounce_seconds(ChannelRole.ASSISTANT)

        if delay_seconds != ASSISTANT_DEBOUNCE_SECONDS:
            self.fail("assistantの待機時間が短い固定値になっていません")

    def test_chat_delay_is_randomized_within_configured_range(self) -> None:
        delay_seconds = get_response_debounce_seconds(ChannelRole.CHAT)

        if not CHAT_DEBOUNCE_MIN_SECONDS <= delay_seconds <= CHAT_DEBOUNCE_MAX_SECONDS:
            self.fail("chatの待機時間が設定範囲を外れています")


class SpontaneousActionCooldownTest(unittest.TestCase):
    def test_text_action_waits_for_text_cooldown(self) -> None:
        now = 1_000.0

        allowed = can_execute_spontaneous_action(
            ResponseAction.REPLY,
            last_action_at=now - CHAT_TEXT_COOLDOWN_SECONDS + 1,
            now=now,
        )

        if allowed:
            self.fail("自発テキスト投稿がクールダウン中にも実行されます")

    def test_reaction_uses_shorter_cooldown(self) -> None:
        now = 1_000.0

        allowed = can_execute_spontaneous_action(
            ResponseAction.REACTION,
            last_action_at=now - CHAT_REACTION_COOLDOWN_SECONDS,
            now=now,
        )

        if not allowed:
            self.fail("リアクションが短いクールダウン後にも実行されません")

    def test_silence_is_not_limited_by_cooldown(self) -> None:
        allowed = can_execute_spontaneous_action(
            ResponseAction.SILENCE,
            last_action_at=999.0,
            now=1_000.0,
        )

        if not allowed:
            self.fail("沈黙がクールダウンによって妨げられています")


class SpontaneousGenerationTest(unittest.TestCase):
    def test_skips_generation_while_reaction_is_on_cooldown(self) -> None:
        can_start = can_start_spontaneous_generation(
            last_action_at=999.0,
            now=1_000.0,
        )

        if can_start:
            self.fail("リアクションも抑制される期間に自発生成を開始します")

    def test_starts_generation_after_reaction_cooldown(self) -> None:
        can_start = can_start_spontaneous_generation(
            last_action_at=1_000.0 - CHAT_REACTION_COOLDOWN_SECONDS,
            now=1_000.0,
        )

        if not can_start:
            self.fail("リアクションが可能な時点で自発生成を開始しません")


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
