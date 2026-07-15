import asyncio
import datetime
from dataclasses import dataclass, field

from discord import Message

from .custom_profile import CustomProfile


@dataclass
class ChannelProcessingState:
    """チャンネルごとの生成状態と生成中に受信したメッセージを保持します。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generating: bool = False
    pending_messages: list[Message] = field(default_factory=list)
    queued_response_message: Message | None = None
    queued_response_is_explicit_call: bool = False
    debounce_task: asyncio.Task[None] | None = None
    debounced_response_message: Message | None = None
    debounced_response_is_explicit_call: bool = False
    generation_revision: int = 0
    last_action_at: float | None = None
    last_human_message_timestamp: datetime.datetime | None = None
    unanswered_question_task: asyncio.Task[None] | None = None
    unanswered_question_message_id: int | None = None
    queued_response_is_unanswered_question: bool = False
    debounced_response_is_unanswered_question: bool = False
    queued_custom_profile: CustomProfile | None = None
    debounced_custom_profile: CustomProfile | None = None
    active_response_message: Message | None = None
    active_response_is_explicit_call: bool = False
    active_response_is_unanswered_question: bool = False
    active_custom_profile: CustomProfile | None = None
