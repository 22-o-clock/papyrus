"""冷笑王ランキングのEmbed生成と、内容変化の検出。"""

import datetime
import hashlib
import io

import discord
from discord.utils import escape_markdown

from cogs.cynicism.constants import RANKING_DISPLAY_LIMIT
from cogs.cynicism.models import CynicismRanking, RankingEntry
from cogs.cynicism.periods import CynicismPeriod, format_period

REPORT_MARKER_PREFIX = "cynicism-report:"
EMBED_COLOR = discord.Color.blue()
PERCENT_SCALE = 100
EMBED_FIELD_VALUE_LIMIT = 1024


def report_marker(period: CynicismPeriod) -> str:
    """発表済みメッセージを履歴から再発見するための識別子を返す。"""
    return f"{REPORT_MARKER_PREFIX}{period.period_type.value}:{period.start_date.isoformat()}"


def format_points(points: int) -> str:
    """冷笑ポイントを整数で表示する。"""
    return str(points)


def format_rate(rate: float) -> str:
    """冷笑率を百分率で表示する。"""
    return f"{rate * PERCENT_SCALE:.1f}%"


def ranking_digest(ranking: CynicismRanking) -> str:
    """内容が変化したかを判定するための指紋を返す。"""
    parts = [
        "rate-primary-v4",
        ranking.period.period_type.value,
        ranking.period.start_date.isoformat(),
        str(ranking.qualification_threshold),
        str(ranking.excluded_channel_count),
    ]
    for section in (ranking.total_entries, ranking.rate_entries):
        parts.extend(
            f"{entry.rank}:{entry.member_id}:{entry.display_name}:{format_points(entry.points)}"
            f":{entry.message_count}:{entry.rate:.6f}"
            for entry in section
        )
    parts.extend(
        f"{top.jump_url}:{top.member_id}:{top.display_name}:{format_points(top.points)}" for top in ranking.top_messages
    )
    parts.extend(f"reactor:{entry.member_id}:{entry.display_name}:{entry.points}" for entry in ranking.reactor_contributions)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def build_empty_notice(period: CynicismPeriod) -> str:
    """対象の🥶が無い期間に返す案内文を返す。"""
    return f"{period.label}冷笑王 ({format_period(period)}): 対象期間に冷笑ポイントは観測されませんでした。"


def build_ranking_embed(ranking: CynicismRanking, *, updated_at: datetime.datetime) -> discord.Embed:
    """冷笑率による王と参考ポイント、最多ポイントの発言をまとめる。"""
    # 期間が終わる前に見た場合は順位が確定していないため、途中経過であることを明示する。
    is_in_progress = updated_at < ranking.period.end_at
    title = f"{ranking.period.label}冷笑王 (集計中)" if is_in_progress else f"{ranking.period.label}冷笑王"
    description = format_period(ranking.period)
    if is_in_progress:
        description += "\n※期間の途中経過です。順位はまだ確定していません。"
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)

    rate_champion = ranking.rate_champion
    if rate_champion is None:
        embed.add_field(
            name="👑 冷笑王 (冷笑率)",
            value=f"該当なし (期間内{ranking.qualification_threshold}発言以上が資格ライン)",
            inline=False,
        )
    else:
        embed.add_field(name="👑 冷笑王 (冷笑率)", value=_format_champion(rate_champion), inline=False)

    if ranking.rate_entries:
        embed.add_field(
            name=f"冷笑率ランキング (期間内{ranking.qualification_threshold}発言以上)",
            value=_format_rate_lines(ranking.rate_entries),
            inline=False,
        )

    if ranking.total_entries:
        embed.add_field(
            name="参考: 合計ポイントランキング",
            value=_format_total_lines(ranking.total_entries),
            inline=False,
        )
    if ranking.top_messages:
        top_text = _format_top_messages(ranking)
        if len(top_text) > EMBED_FIELD_VALUE_LIMIT:
            top_text = (
                f"同率1位 {len(ranking.top_messages)}件 — 各{ranking.top_messages[0].points} pt\n"
                "発言者とリンクの全件一覧は添付ファイルをご覧ください。"
            )
        embed.add_field(name="🥶 最多ポイントの発言", value=top_text, inline=False)

    embed.add_field(name="サマリ", value=_format_summary(ranking), inline=False)
    embed.set_footer(
        text=(f"{report_marker(ranking.period)} | 集計基準=発言の投稿日時 (JST) | 最終更新 {updated_at:%Y-%m-%d %H:%M}")
    )
    return embed


def _format_champion(entry: RankingEntry) -> str:
    """王として表示する1名分の内訳を返す。"""
    return (
        f"**{entry.display_name}** — 冷笑率 {format_rate(entry.rate)}\n"
        f"冷笑ポイント {format_points(entry.points)} pt / 発言 {entry.message_count}件\n"
        f"冷笑認定された発言 {entry.cynical_message_count}件"
    )


def _format_total_lines(entries: tuple[RankingEntry, ...]) -> str:
    """合計部門の上位を1行ずつ整形する。"""
    return "\n".join(
        f"{entry.rank}. {entry.display_name} — {format_points(entry.points)} pt" for entry in entries[:RANKING_DISPLAY_LIMIT]
    )


def _format_rate_lines(entries: tuple[RankingEntry, ...]) -> str:
    """冷笑率部門の上位を1行ずつ整形する。"""
    return "\n".join(
        f"{entry.rank}. {entry.display_name} — {format_rate(entry.rate)} "
        f"({format_points(entry.points)} pt / {entry.message_count}発言)"
        for entry in entries[:RANKING_DISPLAY_LIMIT]
    )


def _format_summary(ranking: CynicismRanking) -> str:
    """総ポイントと付与者別内訳、集計対象人数を返す。長い内訳は添付へ案内する。"""
    total = f"総ポイント {format_points(ranking.total_points)} pt"
    members = (
        f"対象 {ranking.member_count}名 / 資格ライン到達 {ranking.qualified_member_count}名"
        f" / 除外対象 {ranking.excluded_channel_count}件 (チャンネル・スレッド)"
    )
    breakdown = _format_reactor_contributions(ranking)
    if breakdown:
        if len(f"{total} ({breakdown})\n{members}") <= EMBED_FIELD_VALUE_LIMIT:
            total += f" ({breakdown})"
        else:
            total += " (付与者別の全内訳は添付ファイルをご覧ください)"
    return f"{total}\n{members}"


def _format_reactor_contributions(ranking: CynicismRanking) -> str:
    """付与ポイント順の内訳を、メンションせず読点で区切る。"""
    return "、".join(f"{escape_markdown(entry.display_name)} {entry.points}pt" for entry in ranking.reactor_contributions)


def build_report_files(ranking: CynicismRanking) -> list[discord.File]:
    """最多発言と付与者別内訳のうち、Embedに収まらない全件を添付する。"""
    files = build_top_messages_files(ranking)
    breakdown = _format_reactor_contributions(ranking)
    if breakdown and breakdown not in _format_summary(ranking):
        text = "\n".join(f"{entry.display_name}: {entry.points}pt" for entry in ranking.reactor_contributions)
        files.append(discord.File(io.BytesIO(text.encode("utf-8")), filename="cynicism_reactor_points.txt"))
    return files


def _format_top_messages(ranking: CynicismRanking) -> str:
    """同率1位の発言を省略せず整形する。"""
    return "\n".join(
        f"**{top.display_name}** — {format_points(top.points)} pt [発言を見る]({top.jump_url})" for top in ranking.top_messages
    )


def build_top_messages_files(ranking: CynicismRanking) -> list[discord.File]:
    """Embedに収まらない場合に、同率1位の全件一覧を添付する。"""
    text = _format_top_messages(ranking)
    if len(text) <= EMBED_FIELD_VALUE_LIMIT:
        return []
    return [discord.File(io.BytesIO(text.encode("utf-8")), filename="cynicism_top_messages.txt")]
