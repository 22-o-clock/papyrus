import datetime

from cogs.chatbot.constants import HISTORY_SYNC_INITIAL_LOOKBACK_HOURS, HISTORY_SYNC_MAXIMUM_LOOKBACK_DAYS


def get_history_sync_after(latest_stored_at: datetime.datetime | None, now: datetime.datetime) -> datetime.datetime:
    """保存状況に応じて、起動時にDiscord履歴を取得する開始日時を返します。"""
    maximum_lookback = now - datetime.timedelta(days=HISTORY_SYNC_MAXIMUM_LOOKBACK_DAYS)
    if latest_stored_at is None:
        return now - datetime.timedelta(hours=HISTORY_SYNC_INITIAL_LOOKBACK_HOURS)
    return max(latest_stored_at, maximum_lookback)
