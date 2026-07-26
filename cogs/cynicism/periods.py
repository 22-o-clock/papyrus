"""冷笑王を決める集計期間を、JST暦日の境界で表現する。"""

import calendar
import datetime
from dataclasses import dataclass
from enum import StrEnum

from .constants import JST

DECEMBER = 12
LAST_DAY_OF_DECEMBER = 31


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
    """開始日と終了日の両方を含む、JST暦日の集計期間。"""

    period_type: CynicismPeriodType
    start_date: datetime.date
    end_date: datetime.date

    @property
    def start_at(self) -> datetime.datetime:
        """期間開始のJST日時を返す。"""
        return datetime.datetime.combine(self.start_date, datetime.time.min, tzinfo=JST)

    @property
    def end_at(self) -> datetime.datetime:
        """終了日の翌日0時(JST)を返す。集計ではこの時刻を含めない。"""
        next_day = self.end_date + datetime.timedelta(days=1)
        return datetime.datetime.combine(next_day, datetime.time.min, tzinfo=JST)

    @property
    def label(self) -> str:
        """種別の日本語表記を返す。"""
        return PERIOD_LABELS[self.period_type]


def period_containing(period_type: CynicismPeriodType, target: datetime.date) -> CynicismPeriod:
    """指定日を含む期間を返す。期間途中の日付を渡しても正規化された期間になる。"""
    if period_type is CynicismPeriodType.WEEKLY:
        # 週は月曜始まりとする。ISO週番号は年末年始の表記が直感に反するため使わない。
        start_date = target - datetime.timedelta(days=target.weekday())
        return CynicismPeriod(period_type, start_date, start_date + datetime.timedelta(days=6))
    if period_type is CynicismPeriodType.MONTHLY:
        last_day = calendar.monthrange(target.year, target.month)[1]
        return CynicismPeriod(period_type, target.replace(day=1), target.replace(day=last_day))
    return CynicismPeriod(
        period_type,
        datetime.date(target.year, 1, 1),
        datetime.date(target.year, DECEMBER, LAST_DAY_OF_DECEMBER),
    )


def previous_period(period: CynicismPeriod) -> CynicismPeriod:
    """1つ前の同種別の期間を返す。"""
    return period_containing(period.period_type, period.start_date - datetime.timedelta(days=1))


def current_period(period_type: CynicismPeriodType, now: datetime.datetime) -> CynicismPeriod:
    """現在時刻(JST)が属する、まだ終了していない期間を返す。"""
    return period_containing(period_type, now.astimezone(JST).date())


def latest_completed_period(period_type: CynicismPeriodType, now: datetime.datetime) -> CynicismPeriod:
    """現在時刻(JST)から見て直近の、終了済みの期間を返す。"""
    return previous_period(current_period(period_type, now))


def qualification_threshold(period: CynicismPeriod) -> int:
    """平均部門の対象となるために必要な、期間内の発言数を返す。"""
    return QUALIFICATION_MESSAGE_COUNTS[period.period_type]


def format_period(period: CynicismPeriod) -> str:
    """期間を利用者向けの日付範囲表記へ変換する。"""
    return f"{period.start_date.isoformat()} 〜 {period.end_date.isoformat()} (JST)"
