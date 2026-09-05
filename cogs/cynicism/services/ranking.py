"""冷笑ポイントと、合計・冷笑率2部門のランキング組み立て。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from cogs.cynicism.models import (
    CynicismRanking,
    MemberReactionCounts,
    RankedMemberIdentity,
    RankingEntry,
)
from cogs.cynicism.periods import CynicismPeriod, qualification_threshold


def cynicism_rate(points: int, message_count: int) -> float:
    """発言1件あたりの冷笑ポイントを返す。発言が無い場合は0とする。"""
    if message_count <= 0:
        return 0.0
    return float(points) / message_count


def build_ranking(
    period: CynicismPeriod,
    counts: Sequence[MemberReactionCounts],
    message_counts: Mapping[int, int],
    identities: Mapping[int, RankedMemberIdentity],
) -> CynicismRanking:
    """合計部門と、資格ラインを適用した冷笑率部門を組み立てる。"""
    threshold = qualification_threshold(period)
    rows: list[RankingEntry] = []
    for member_counts in counts:
        identity = identities.get(member_counts.member_id)
        # Botの発言は競争の対象にしない。talkdata側にBot判定が無いためここで落とす。
        if identity is not None and identity.is_bot:
            continue
        points = member_counts.human_count
        if points <= 0:
            continue
        message_count = message_counts.get(member_counts.member_id, 0)
        display_name = identity.display_name if identity is not None else str(member_counts.member_id)
        rows.append(
            RankingEntry(
                rank=0,
                member_id=member_counts.member_id,
                display_name=display_name,
                points=points,
                human_count=member_counts.human_count,
                cynical_message_count=member_counts.cynical_message_count,
                message_count=message_count,
                rate=cynicism_rate(points, message_count),
            )
        )

    total_entries = _ranked(rows, lambda entry: float(entry.points))
    qualified = [entry for entry in rows if entry.message_count >= threshold]
    rate_entries = _ranked(qualified, lambda entry: entry.rate)
    return CynicismRanking(
        period=period,
        total_entries=total_entries,
        rate_entries=rate_entries,
        qualification_threshold=threshold,
        total_points=sum(entry.points for entry in rows),
        human_reaction_count=sum(entry.human_count for entry in rows),
        member_count=len(rows),
    )


def _ranked(entries: Sequence[RankingEntry], sort_key: Callable[[RankingEntry], float]) -> tuple[RankingEntry, ...]:
    """降順に並べ、同値へ同順位を割り当てる。"""
    ordered = sorted(entries, key=lambda entry: (-sort_key(entry), entry.display_name, entry.member_id))
    ranked: list[RankingEntry] = []
    previous_value: float | None = None
    rank = 0
    for position, entry in enumerate(ordered, start=1):
        value = sort_key(entry)
        if previous_value is None or value != previous_value:
            rank = position
            previous_value = value
        ranked.append(replace(entry, rank=rank))
    return tuple(ranked)
