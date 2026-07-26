import datetime
import random

from discord import Message, MessageReference

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.constants import (
    ASSISTANT_DEBOUNCE_SECONDS,
    CHAT_DEBOUNCE_MAX_SECONDS,
    CHAT_DEBOUNCE_MIN_SECONDS,
)
from cogs.chatbot.models.conversation import ChannelProcessingState
from cogs.chatbot.models.custom_profile import CustomProfile


def activate_response(
    state: ChannelProcessingState,
    message: Message,
    *,
    is_explicit_call: bool,
    custom_profile: CustomProfile | None,
) -> None:
    """現在処理中の応答要求と、その必須性を状態へ保持します。"""
    state.active_response_message = message
    state.active_response_is_explicit_call = is_explicit_call
    state.active_custom_profile = custom_profile


def claim_response_slot(
    state: ChannelProcessingState,
    message: Message,
    *,
    is_explicit_call: bool,
    custom_profile: CustomProfile | None = None,
) -> bool:
    """生成枠を確保し、使用中の場合は次の返信対象としてメッセージを保持します。"""
    if state.generating:
        if is_explicit_call:
            state.queued_response_message = message
            state.queued_response_is_explicit_call = True
            state.queued_custom_profile = custom_profile
        elif not state.queued_response_is_explicit_call:
            if state.active_response_is_explicit_call and state.active_response_message is not None:
                state.queued_response_message = state.active_response_message
                state.queued_response_is_explicit_call = True
                state.queued_custom_profile = state.active_custom_profile
            else:
                state.queued_response_message = message
                state.queued_response_is_explicit_call = False
                state.queued_custom_profile = custom_profile
        state.generation_revision += 1
        return False
    state.generating = True
    activate_response(
        state,
        message,
        is_explicit_call=is_explicit_call,
        custom_profile=custom_profile,
    )
    return True


def get_response_debounce_seconds(role: ChannelRole) -> float:
    """役割に応じた返信生成前の待機秒数を返します。"""
    if role is ChannelRole.ASSISTANT:
        return ASSISTANT_DEBOUNCE_SECONDS
    return random.SystemRandom().uniform(CHAT_DEBOUNCE_MIN_SECONDS, CHAT_DEBOUNCE_MAX_SECONDS)


def is_generation_current(state: ChannelProcessingState, revision: int) -> bool:
    """生成開始後に、回答を作り直す必要がある返信要求が追加されていないか確認します。"""
    return state.generation_revision == revision


def should_reset_conversation(
    last_human_message_timestamp: datetime.datetime | None,
    current_message_timestamp: datetime.datetime,
    reset_minutes: int,
) -> bool:
    """最後の人間投稿から設定時間以上空いたときに会話文脈をリセットするか判定します。"""
    if last_human_message_timestamp is None:
        return False
    return current_message_timestamp - last_human_message_timestamp >= datetime.timedelta(minutes=reset_minutes)


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
