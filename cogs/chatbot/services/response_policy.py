import datetime
import random

from discord import Message, MessageReference

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.constants import (
    ASSISTANT_DEBOUNCE_SECONDS,
    CHAT_DEBOUNCE_MAX_SECONDS,
    CHAT_DEBOUNCE_MIN_SECONDS,
    CHAT_REACTION_COOLDOWN_SECONDS,
    CHAT_TEXT_COOLDOWN_SECONDS,
    QUESTION_ENDING_PATTERN,
)
from cogs.chatbot.models.conversation import ChannelProcessingState
from cogs.chatbot.models.custom_profile import CustomProfile
from cogs.chatbot.responses_api import ResponseAction


def claim_response_slot(
    state: ChannelProcessingState,
    message: Message,
    *,
    is_explicit_call: bool,
    is_unanswered_question: bool,
    custom_profile: CustomProfile | None = None,
) -> bool:
    """生成枠を確保し、使用中の場合は次の返信対象としてメッセージを保持します。"""
    if state.generating:
        state.queued_response_message = message
        state.queued_response_is_explicit_call = is_explicit_call
        state.queued_response_is_unanswered_question = is_unanswered_question
        state.queued_custom_profile = custom_profile
        state.generation_revision += 1
        return False
    state.generating = True
    return True


def get_response_debounce_seconds(role: ChannelRole) -> float:
    """役割に応じた返信生成前の待機秒数を返します。"""
    if role is ChannelRole.ASSISTANT:
        return ASSISTANT_DEBOUNCE_SECONDS
    return random.SystemRandom().uniform(CHAT_DEBOUNCE_MIN_SECONDS, CHAT_DEBOUNCE_MAX_SECONDS)


def is_generation_current(state: ChannelProcessingState, revision: int) -> bool:
    """生成開始後に、回答を作り直す必要がある返信要求が追加されていないか確認します。"""
    return state.generation_revision == revision


def can_execute_spontaneous_action(action: ResponseAction, last_action_at: float | None, now: float) -> bool:
    """自発反応が行動別のクールダウンを過ぎているか判定します。"""
    if action is ResponseAction.SILENCE or last_action_at is None:
        return True
    cooldown_seconds = CHAT_REACTION_COOLDOWN_SECONDS if action is ResponseAction.REACTION else CHAT_TEXT_COOLDOWN_SECONDS
    return now - last_action_at >= cooldown_seconds


def can_start_spontaneous_generation(last_action_at: float | None, now: float) -> bool:
    """全ての自発行動が抑制される期間を避けて生成を始めるか判定します。"""
    return last_action_at is None or now - last_action_at >= CHAT_REACTION_COOLDOWN_SECONDS


def should_reset_conversation(
    last_human_message_timestamp: datetime.datetime | None,
    current_message_timestamp: datetime.datetime,
    reset_minutes: int,
) -> bool:
    """最後の人間投稿から設定時間以上空いたときに会話文脈をリセットするか判定します。"""
    if last_human_message_timestamp is None:
        return False
    return current_message_timestamp - last_human_message_timestamp >= datetime.timedelta(minutes=reset_minutes)


def is_unaddressed_question(*, content: str, is_reply: bool, mentioned_user_ids: list[int]) -> bool:
    """宛先のない質問として待機対象にする投稿か判定します。"""
    if is_reply or mentioned_user_ids:
        return False
    normalized_content = content.replace("\uff1f", "?").strip()
    return QUESTION_ENDING_PATTERN.search(normalized_content) is not None


def get_unanswered_question_wait_minutes(minimum_minutes: int, maximum_minutes: int) -> int:
    """宛先のない質問への回答を待つ時間を一様ランダムに選びます。"""
    return random.SystemRandom().randint(minimum_minutes, maximum_minutes)


def can_change_channel_role(*, is_thread: bool, manage_channels: bool) -> bool:
    """スレッドでは全員、通常チャンネルでは管理権限を持つ人だけに変更を許可します。"""
    return is_thread or manage_channels


def get_available_referenced_author_id(reference: MessageReference) -> int | None:
    """追加のAPI取得なしで利用できる返信元メッセージの発言者IDを返します。"""
    if isinstance(reference.resolved, Message):
        return reference.resolved.author.id
    if reference.cached_message is not None:
        return reference.cached_message.author.id
    return None


def should_respond(role: ChannelRole, *, mentioned_bot: bool, replied_to_bot: bool) -> bool:
    """チャンネル役割と呼びかけ方法から、応答判断を開始するか決定します。"""
    return mentioned_bot or replied_to_bot or role is ChannelRole.CHAT
