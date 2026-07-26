"""期間ごとの発表時刻と、発表・再集計の対象期間の決定。"""

import datetime

from cogs.cynicism.constants import PUBLISH_HOUR
from cogs.cynicism.periods import CynicismPeriod, CynicismPeriodType, latest_completed_period, previous_period

# 起動していなかった期間を補うために遡る上限。
MAXIMUM_BACKFILL_PERIODS = {
    CynicismPeriodType.WEEKLY: 8,
    CynicismPeriodType.MONTHLY: 3,
    CynicismPeriodType.YEARLY: 1,
}

# 遅れて付いた🥶や重み変更を反映するため、発表済みでも再集計する期間数。
REFRESH_PERIOD_COUNTS = {
    CynicismPeriodType.WEEKLY: 4,
    CynicismPeriodType.MONTHLY: 2,
    CynicismPeriodType.YEARLY: 1,
}


def publish_time(period: CynicismPeriod) -> datetime.datetime:
    """期間終了の翌日 PUBLISH_HOUR (JST) を返す。"""
    return period.end_at + datetime.timedelta(hours=PUBLISH_HOUR)


def publishable_periods(
    now: datetime.datetime,
    earliest_recorded: datetime.date | None,
) -> tuple[CynicismPeriod, ...]:
    """発表時刻を過ぎ、遡及上限と記録開始日の範囲にある期間を古い順に返す。"""
    periods: list[CynicismPeriod] = []
    for period_type in CynicismPeriodType:
        collected: list[CynicismPeriod] = []
        period = latest_completed_period(period_type, now)
        for _ in range(MAXIMUM_BACKFILL_PERIODS[period_type]):
            # 記録開始より前の期間は集計対象が存在しないため遡らない。
            if earliest_recorded is not None and period.end_date < earliest_recorded:
                break
            if publish_time(period) <= now:
                collected.append(period)
            period = previous_period(period)
        periods.extend(reversed(collected))
    return tuple(periods)


def refreshable_periods(now: datetime.datetime) -> tuple[CynicismPeriod, ...]:
    """発表済みの内容を更新する対象期間を返す。"""
    periods: list[CynicismPeriod] = []
    for period_type in CynicismPeriodType:
        period = latest_completed_period(period_type, now)
        for _ in range(REFRESH_PERIOD_COUNTS[period_type]):
            periods.append(period)
            period = previous_period(period)
    return tuple(periods)
