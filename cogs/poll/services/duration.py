"""投票期間の指定を締め切り日時へ変換する。"""

import datetime
import re

from cogs.poll.constants import JST, MIN_DURATION
from core.exception import ArgumentError

RELATIVE_PATTERN = re.compile(r"^(?:(\d{1,4})d)?(?:(\d{1,5})h)?(?:(\d{1,6})m)?$")
ABSOLUTE_PATTERN = re.compile(r"^(?:(\d{1,2})/(\d{1,2})\s+)?(\d{1,2}):(\d{2})$")
FORMAT_MESSAGE = (
    "投票期間は `1d12h`、`3h`、`30m` のような長さか、`09/30 21:00`、`21:00` のような締め切り日時(JST)で指定してください... 💦"
)


def parse_deadline(value: str, now: datetime.datetime) -> datetime.datetime:
    """投票期間の指定から締め切り日時を求める。

    時刻だけを指定した場合は、次にその時刻になる日時を締め切りとする。月日を指定して過去になる場合は翌年とする。

    Raises:
        ArgumentError: 形式が不正な場合、存在しない日時の場合、期間が短すぎる場合。

    """
    text = value.strip().lower()
    compact = re.sub(r"\s+", "", text)
    if (match := RELATIVE_PATTERN.match(compact)) and any(match.groups()):
        days, hours, minutes = (int(group) if group else 0 for group in match.groups())
        deadline = now + datetime.timedelta(days=days, hours=hours, minutes=minutes)
    elif match := ABSOLUTE_PATTERN.match(text):
        deadline = _next_absolute_deadline(match, now.astimezone(JST))
    else:
        raise ArgumentError(FORMAT_MESSAGE)

    if deadline - now < MIN_DURATION:
        message = "投票期間は1分以上にしてください... 💦"
        raise ArgumentError(message)
    return deadline


def _next_absolute_deadline(match: re.Match[str], now: datetime.datetime) -> datetime.datetime:
    """月日と時刻の指定から、現在より後の締め切り日時を求める。"""
    month, day, hour, minute = match.groups()
    try:
        if month is None or day is None:
            deadline = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if deadline <= now:
                deadline += datetime.timedelta(days=1)
            return deadline
        deadline = now.replace(month=int(month), day=int(day), hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if deadline <= now:
            deadline = deadline.replace(year=now.year + 1)
    except ValueError:
        message = "存在しない日時が指定されました... 💦"
        raise ArgumentError(message) from None
    return deadline
