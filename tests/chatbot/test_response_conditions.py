import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from discord import Message, MessageReference

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.chatbot_cog import (
    ChannelProcessingState,
    can_change_channel_role,
    claim_response_slot,
    get_available_referenced_author_id,
    should_respond,
)


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
            spontaneous_chat_reply=False,
        )

        if not result:
            self.fail("assistantがメンションへ応答しません")

    def test_assistant_responds_to_reply(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=True,
            spontaneous_chat_reply=False,
        )

        if not result:
            self.fail("assistantがボットへの返信へ応答しません")

    def test_assistant_ignores_spontaneous_reply_decision(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=True,
        )

        if result:
            self.fail("assistantが明示的に呼ばれていない投稿へ応答します")

    def test_chat_can_respond_spontaneously(self) -> None:
        result = should_respond(
            ChannelRole.CHAT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=True,
        )

        if not result:
            self.fail("chatが自発返信の判定を反映しません")

    def test_chat_ignores_when_not_called_and_spontaneous_decision_is_false(self) -> None:
        result = should_respond(
            ChannelRole.CHAT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=False,
        )

        if result:
            self.fail("chatが返信不要の判定でも応答します")


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

        first_claimed = claim_response_slot(first_state, first_message)
        second_claimed = claim_response_slot(second_state, second_message)

        if not first_claimed or not second_claimed:
            self.fail("別チャンネルの生成枠が互いに干渉しています")

    def test_same_channel_queues_latest_response_request(self) -> None:
        state = ChannelProcessingState()
        first_message = make_discord_message(author_id=100)
        second_message = make_discord_message(author_id=200)

        first_claimed = claim_response_slot(state, first_message)
        second_claimed = claim_response_slot(state, second_message)

        if not first_claimed:
            self.fail("最初の返信要求が生成枠を確保できません")
        if second_claimed:
            self.fail("同じチャンネルで生成枠を二重に確保しています")
        if state.queued_response_message is not second_message:
            self.fail("生成中に受けた返信要求を次回処理へ保持していません")

    def test_channel_states_do_not_share_pending_messages(self) -> None:
        first_state = ChannelProcessingState()
        second_state = ChannelProcessingState()
        first_state.pending_messages.append(make_discord_message(author_id=100))

        if second_state.pending_messages:
            self.fail("別チャンネル間で保留メッセージを共有しています")


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
