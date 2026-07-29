"""冷笑王を決める集計期間を、JSTの境界で表現する。"""

import calendar
import datetime
from dataclasses import dataclass
from enum import StrEnum

from cogs.cynicism.constants import JST

DECEMBER = 12
LAST_DAY_OF_DECEMBER = 31

# 週は金曜22時(JST)で切り替える。曜日はMonday=0。
WEEK_START_WEEKDAY = 4
WEEK_START_HOUR = 22


class CynicismPeriodType(StrEnum):
    """集計期間の種別。"""

    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


# 平均部門(冷笑率)の対象とするために必要な、期間内の発言数。
QUALIFICATION_MESSAGE_COUNTS = {
    CynicismPeriodType.WEEKLY: 10,
    CynicismPeriodType.MONTHLY: 30,
    CynicismPeriodType.YEARLY: 100,
}

PERIOD_LABELS = {
    CynicismPeriodType.WEEKLY: "週間",
    CynicismPeriodType.MONTHLY: "月間",
    CynicismPeriodType.YEARLY: "年間",
}


@dataclass(frozen=True, slots=True)
class CynicismPeriod:
    """開始時刻を含み、終了時刻を含まないJSTの集計期間。"""

    period_type: CynicismPeriodType
    start_at: datetime.datetime
    end_at: datetime.datetime

    @property
    def start_date(self) -> datetime.date:
        """配送記録のキーと表示に使う開始日を返す。"""
        return self.start_at.date()

    @property
    def end_date(self) -> datetime.date:
        """表示に使う最終日を返す。終了時刻は含まないため、その直前の日付を返す。"""
        return (self.end_at - datetime.timedelta(microseconds=1)).date()

    @property
    def label(self) -> str:
        """種別の日本語表記を返す。"""
        return PERIOD_LABELS[self.period_type]


def period_containing(period_type: CynicismPeriodType, moment: datetime.datetime) -> CynicismPeriod:
    """指定時刻が属する期間を返す。期間途中の時刻を渡しても正規化された期間になる。"""
    local = moment.astimezone(JST)
    if period_type is CynicismPeriodType.WEEKLY:
        start_at = _weekly_start(local)
        return CynicismPeriod(period_type, start_at, start_at + datetime.timedelta(days=7))
    if period_type is CynicismPeriodType.MONTHLY:
        start_at = _midnight(local.date().replace(day=1))
        last_day = calendar.monthrange(local.year, local.month)[1]
        return CynicismPeriod(period_type, start_at, _midnight(local.date().replace(day=last_day) + _ONE_DAY))
    start_at = _midnight(datetime.date(local.year, 1, 1))
    return CynicismPeriod(period_type, start_at, _midnight(datetime.date(local.year + 1, 1, 1)))


def previous_period(period: CynicismPeriod) -> CynicismPeriod:
    """1つ前の同種別の期間を返す。"""
    return period_containing(period.period_type, period.start_at - datetime.timedelta(microseconds=1))


def current_period(period_type: CynicismPeriodType, now: datetime.datetime) -> CynicismPeriod:
    """現在時刻が属する、まだ終了していない期間を返す。"""
    return period_containing(period_type, now)


def latest_completed_period(period_type: CynicismPeriodType, now: datetime.datetime) -> CynicismPeriod:
    """現在時刻から見て直近の、終了済みの期間を返す。"""
    return previous_period(current_period(period_type, now))


def period_from_start_date(period_type: CynicismPeriodType, start: datetime.date) -> CynicismPeriod:
    """利用者が指定した日付を含む期間を返す。

    週の切り替え時刻に合わせて解釈するため、表示された開始日を渡すとその期間になる。
    """
    return period_containing(period_type, _week_switch_time(start))


def qualification_threshold(period: CynicismPeriod) -> int:
    """平均部門の対象となるために必要な、期間内の発言数を返す。"""
    return QUALIFICATION_MESSAGE_COUNTS[period.period_type]


def format_period(period: CynicismPeriod) -> str:
    """期間を利用者向けの表記へ変換する。"""
    if period.period_type is CynicismPeriodType.WEEKLY:
        # 週は日付の途中で切り替わるため、時刻まで示さないと範囲が分からない。
        return f"{period.start_at:%Y-%m-%d %H:%M} 〜 {period.end_at:%Y-%m-%d %H:%M} (JST)"
    return f"{period.start_date.isoformat()} 〜 {period.end_date.isoformat()} (JST)"


_ONE_DAY = datetime.timedelta(days=1)


def _midnight(day: datetime.date) -> datetime.datetime:
    """JST暦日の0時を返す。"""
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=JST)


def _week_switch_time(day: datetime.date) -> datetime.datetime:
    """JST暦日の週切り替え時刻を返す。"""
    return datetime.datetime.combine(day, datetime.time(hour=WEEK_START_HOUR), tzinfo=JST)


def _weekly_start(local: datetime.datetime) -> datetime.datetime:
    """指定時刻が属する週の開始(直近の金曜22時 JST)を返す。"""
    switch_time = _week_switch_time(local.date())
    start_at = switch_time - datetime.timedelta(days=(local.weekday() - WEEK_START_WEEKDAY) % 7)
    if start_at > local:
        # 金曜の切り替え前は、まだ前の週に属する。
        start_at -= datetime.timedelta(days=7)
    return start_at
