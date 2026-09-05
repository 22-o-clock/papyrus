import datetime

from discord import Message, MessageReference


def should_reset_conversation(
    last_human_message_timestamp: datetime.datetime | None,
    current_message_timestamp: datetime.datetime,
    reset_minutes: int,
) -> bool:
    """最後の人間投稿から設定時間以上空いたときに会話文脈をリセットするか判定します。"""
    if last_human_message_timestamp is None:
        return False
    return current_message_timestamp - last_human_message_timestamp >= datetime.timedelta(minutes=reset_minutes)


def get_available_referenced_author_id(reference: MessageReference) -> int | None:
    """追加のAPI取得なしで利用できる返信元メッセージの発言者IDを返します。"""
    if isinstance(reference.resolved, Message):
        return reference.resolved.author.id
    if reference.cached_message is not None:
        return reference.cached_message.author.id
    return None
