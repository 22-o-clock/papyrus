"""冷笑王ランキングのEmbed生成と、内容変化の検出。"""

import datetime
import hashlib
from decimal import Decimal

import discord

from cogs.cynicism.constants import RANKING_DISPLAY_LIMIT
from cogs.cynicism.models import CynicismRanking, RankingEntry
from cogs.cynicism.periods import CynicismPeriod, format_period

REPORT_MARKER_PREFIX = "cynicism-report:"
EMBED_COLOR = discord.Color.blue()
PERCENT_SCALE = 100


def report_marker(period: CynicismPeriod) -> str:
    """発表済みメッセージを履歴から再発見するための識別子を返す。"""
    return f"{REPORT_MARKER_PREFIX}{period.period_type.value}:{period.start_date.isoformat()}"


def format_points(points: Decimal) -> str:
    """冷笑ポイントを小数第1位まで揃えて表示する。"""
    return f"{points:.1f}"


def format_rate(rate: float) -> str:
    """冷笑率を百分率で表示する。"""
    return f"{rate * PERCENT_SCALE:.1f}%"


def format_weight(weight: Decimal) -> str:
    """重みを小数第1位まで揃えて表示する。"""
    return f"{weight:.1f}"


def ranking_digest(ranking: CynicismRanking) -> str:
    """内容が変化したかを判定するための指紋を返す。"""
    parts = [
        ranking.period.period_type.value,
        ranking.period.start_date.isoformat(),
        format_weight(ranking.weights.papyrus),
        format_weight(ranking.weights.human),
        str(ranking.qualification_threshold),
    ]
    for section in (ranking.total_entries, ranking.rate_entries):
        parts.extend(
            f"{entry.rank}:{entry.member_id}:{entry.display_name}:{format_points(entry.points)}"
            f":{entry.message_count}:{entry.rate:.6f}"
            for entry in section
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def build_empty_notice(period: CynicismPeriod) -> str:
    """対象の🥶が無い期間に返す案内文を返す。"""
    return f"{period.label}冷笑王 ({format_period(period)}): 対象期間に冷笑ポイントは観測されませんでした。"


def build_ranking_embed(ranking: CynicismRanking, *, updated_at: datetime.datetime) -> discord.Embed:
    """2部門の王とランキング表を1つのEmbedへまとめる。"""
    # 期間が終わる前に見た場合は順位が確定していないため、途中経過であることを明示する。
    is_in_progress = updated_at < ranking.period.end_at
    title = f"{ranking.period.label}冷笑王 (集計中)" if is_in_progress else f"{ranking.period.label}冷笑王"
    description = format_period(ranking.period)
    if is_in_progress:
        description += "\n※期間の途中経過です。順位はまだ確定していません。"
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)

    total_champion = ranking.total_champion
    if total_champion is not None:
        embed.add_field(name="👑 冷笑王 (合計)", value=_format_champion(total_champion), inline=False)

    rate_champion = ranking.rate_champion
    if rate_champion is None:
        embed.add_field(
            name="🧊 冷笑率王 (平均)",
            value=f"該当なし (期間内{ranking.qualification_threshold}発言以上が資格ライン)",
            inline=False,
        )
    else:
        embed.add_field(name="🧊 冷笑率王 (平均)", value=_format_champion(rate_champion), inline=False)

    if ranking.total_entries:
        embed.add_field(
            name="合計ランキング",
            value=_format_total_lines(ranking.total_entries),
            inline=False,
        )
    if ranking.rate_entries:
        embed.add_field(
            name=f"冷笑率ランキング (期間内{ranking.qualification_threshold}発言以上)",
            value=_format_rate_lines(ranking.rate_entries),
            inline=False,
        )

    embed.add_field(name="サマリ", value=_format_summary(ranking), inline=False)
    embed.set_footer(
        text=(
            f"{report_marker(ranking.period)} | "
            f"重み Papyrus {format_weight(ranking.weights.papyrus)} / 人間 {format_weight(ranking.weights.human)} | "
            "集計基準=発言の投稿日時 (JST) | "
            f"最終更新 {updated_at:%Y-%m-%d %H:%M}"
        )
    )
    return embed


def _format_champion(entry: RankingEntry) -> str:
    """王として表示する1名分の内訳を返す。"""
    return (
        f"**{entry.display_name}** — {format_points(entry.points)} pt\n"
        f"冷笑認定された発言 {entry.cynical_message_count}件 / 発言 {entry.message_count}件 / "
        f"冷笑率 {format_rate(entry.rate)}\n"
        f"内訳: Papyrus {entry.papyrus_count}件 / 人間 {entry.human_count}件"
    )


def _format_total_lines(entries: tuple[RankingEntry, ...]) -> str:
    """合計部門の上位を1行ずつ整形する。"""
    return "\n".join(
        f"{entry.rank}. {entry.display_name} — {format_points(entry.points)} pt "
        f"(Papyrus {entry.papyrus_count} / 人間 {entry.human_count})"
        for entry in entries[:RANKING_DISPLAY_LIMIT]
    )


def _format_rate_lines(entries: tuple[RankingEntry, ...]) -> str:
    """冷笑率部門の上位を1行ずつ整形する。"""
    return "\n".join(
        f"{entry.rank}. {entry.display_name} — {format_rate(entry.rate)} "
        f"({format_points(entry.points)} pt / {entry.message_count}発言)"
        for entry in entries[:RANKING_DISPLAY_LIMIT]
    )


def _format_summary(ranking: CynicismRanking) -> str:
    """全体の集計値と、冷笑率の読み方の注意を返す。"""
    return (
        f"総ポイント {format_points(ranking.total_points)} pt "
        f"(Papyrus判定 {ranking.papyrus_reaction_count}件 / 人間 {ranking.human_reaction_count}件)\n"
        f"対象 {ranking.member_count}名 / 資格ライン到達 {ranking.qualified_member_count}名\n"
        "※冷笑率は重み付きポイントを発言数で割った値のため、100%を超えることがあります。"
        "重みを変更すると過去の集計値も変わります。"
    )
