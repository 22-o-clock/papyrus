import unittest
import uuid
from datetime import UTC, datetime, timedelta
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
    get_history_sync_after,
    get_latest_memory_search_query,
    get_response_debounce_seconds,
    get_unanswered_question_wait_minutes,
    is_generation_current,
    is_unaddressed_question,
    parse_memory_admin_expiration,
    parse_memory_admin_target,
    should_reset_conversation,
    should_respond,
    validate_exported_memory_ids,
)
from cogs.chatbot.responses_api import MessageInMemory, ResponseAction


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


class MemoryAdminInputTest(unittest.TestCase):
    def test_resolves_selected_member(self) -> None:
        result = parse_memory_admin_target("メンバー", "テストユーザー (123)", {"テストユーザー (123)": 123})

        if result != (123, None, "member"):
            self.fail("Excelで選択したメンバーを対象ユーザーIDへ変換できません")

    def test_rejects_shared_target_with_value(self) -> None:
        try:
            parse_memory_admin_target("共有情報", "不要な対象", {})
        except ValueError:
            return
        self.fail("共有情報に不要な対象名が指定されても拒否されません")

    def test_interprets_naive_excel_datetime_as_japan_time(self) -> None:
        excel_datetime = datetime(2026, 7, 12, 21, 0, tzinfo=UTC).replace(tzinfo=None)
        result = parse_memory_admin_expiration(excel_datetime)

        if result != datetime(2026, 7, 12, 12, 0, tzinfo=UTC):
            self.fail("Excelの日本時間をUTCへ正しく変換できません")

    def test_accepts_exported_subset_when_database_has_new_memories(self) -> None:
        exported_id = uuid.uuid4()

        validate_exported_memory_ids({exported_id}, {exported_id})

    def test_rejects_deleted_exported_memory_row(self) -> None:
        try:
            validate_exported_memory_ids(set(), {uuid.uuid4()})
        except ValueError:
            return
        self.fail("出力後に削除された記憶行が拒否されません")


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

        first_claimed = claim_response_slot(
            first_state,
            first_message,
            is_explicit_call=False,
            is_unanswered_question=False,
        )
        second_claimed = claim_response_slot(
            second_state,
            second_message,
            is_explicit_call=False,
            is_unanswered_question=False,
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
            is_unanswered_question=False,
        )
        second_claimed = claim_response_slot(
            state,
            second_message,
            is_explicit_call=True,
            is_unanswered_question=False,
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


class UnaddressedQuestionTest(unittest.TestCase):
    def test_detects_question_mark_and_japanese_question_ending(self) -> None:
        question_mark_result = is_unaddressed_question(
            content="これは何ですか\uff1f",
            is_reply=False,
            mentioned_user_ids=[],
        )
        ending_result = is_unaddressed_question(
            content="これは何ですか",
            is_reply=False,
            mentioned_user_ids=[],
        )

        if not question_mark_result or not ending_result:
            self.fail("宛先のない疑問文を待機対象として検出できません")

    def test_ignores_question_addressed_to_user_or_reply_target(self) -> None:
        mentioned_user_result = is_unaddressed_question(
            content="@誰か これは何ですか\uff1f",
            is_reply=False,
            mentioned_user_ids=[100],
        )
        reply_result = is_unaddressed_question(
            content="これは何ですか\uff1f",
            is_reply=True,
            mentioned_user_ids=[],
        )

        if mentioned_user_result or reply_result:
            self.fail("宛先のある質問を待機対象にしています")

    def test_selects_wait_within_configured_range(self) -> None:
        wait_minutes = get_unanswered_question_wait_minutes(1, 2)

        if wait_minutes not in (1, 2):
            self.fail("質問への待機時間が設定範囲を外れています")


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
