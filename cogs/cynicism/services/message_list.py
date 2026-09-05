"""冷笑ポイントを獲得した発言をコンパクトなEmbedへ整形する。"""

from collections.abc import Mapping, Sequence

import discord
from discord.utils import escape_markdown

from cogs.cynicism.constants import JST
from cogs.cynicism.models import CynicismMessageRecord, RankedMemberIdentity
from cogs.cynicism.periods import CynicismPeriod, format_period

MESSAGE_PREVIEW_LENGTH = 160
EMBED_DESCRIPTION_LIMIT = 4096
REACTOR_NAMES_LENGTH_LIMIT = 600


def build_message_embeds(
    records: Sequence[CynicismMessageRecord],
    *,
    period: CynicismPeriod,
    display_name: str,
    identities: Mapping[int, RankedMemberIdentity],
    guild_id: int,
) -> list[discord.Embed]:
    """発言を日時リンク・本文抜粋・得点の1行にまとめ、Embedへ分割する。

    Args:
        records: 集計条件に一致する発言。渡された順序で全件を紹介する。
        period: 見出しに表示する対象期間。
        display_name: 発言者の表示名。
        identities: リアクターの表示名。未解決ならIDを表示する。
        guild_id: 元の発言へのリンクに使うサーバーID。

    Returns:
        説明欄の文字数上限に合わせて分割したEmbed一覧。本文の改行は空白にまとめ、160文字を超える部分を
        省略する。日時はJSTの月日・時分で表示し、リンクから元の全文を参照できる。
        対象がない場合は空のリストを返す。

    """
    embeds: list[discord.Embed] = []
    lines: list[str] = []
    header = format_period(period)
    for record in records:
        line = _format_message_line(record, identities=identities, guild_id=guild_id)
        if lines and len("\n".join([header, *lines, line])) > EMBED_DESCRIPTION_LIMIT:
            embeds.append(_build_embed(display_name, period, header, lines))
            lines = []
        lines.append(line)
    if lines:
        embeds.append(_build_embed(display_name, period, header, lines))
    for page, embed in enumerate(embeds, start=1):
        embed.set_footer(text=f"{page}/{len(embeds)}ページ · 全{len(records)}件 · 日時はJST · 全文は日時のリンクから")
    return embeds


def _format_message_line(
    record: CynicismMessageRecord, *, identities: Mapping[int, RankedMemberIdentity], guild_id: int
) -> str:
    """日時リンク、かぎ括弧付きの本文抜粋、得点とリアクターを箇条書きの1行にまとめる。"""
    content = " ".join(record.content.split()) or "(本文なし)"
    if len(content) > MESSAGE_PREVIEW_LENGTH:
        content = content[:MESSAGE_PREVIEW_LENGTH] + "…"
    url = f"https://discord.com/channels/{guild_id}/{record.channel_id}/{record.message_id}"
    reactors = _format_reactors(record.reactor_ids, identities)
    return (
        f"- [{record.post_time.astimezone(JST):%m/%d %H:%M}]({url}) "
        f"「{escape_markdown(content)}」 **{len(record.reactor_ids)}pt** ({reactors})"
    )


def _build_embed(display_name: str, period: CynicismPeriod, header: str, lines: list[str]) -> discord.Embed:
    """期間の見出しと発言の行を、空行を挟まずにEmbedへまとめる。"""
    return discord.Embed(
        title=f"{display_name} の{period.label}冷笑ポイント",
        description="\n".join([header, *lines]),
        color=discord.Color.blue(),
    )


def _format_reactors(reactor_ids: tuple[int, ...], identities: Mapping[int, RankedMemberIdentity]) -> str:
    """リアクターを読点で区切る。極端に長い場合は残りの人数を示す。"""
    names: list[str] = []
    for index, reactor_id in enumerate(reactor_ids):
        name = escape_markdown(identities[reactor_id].display_name) if reactor_id in identities else str(reactor_id)
        if len("、".join([*names, name])) > REACTOR_NAMES_LENGTH_LIMIT:
            names.append(f"ほか{len(reactor_ids) - index}名")
            break
        names.append(name)
    return "、".join(names)
