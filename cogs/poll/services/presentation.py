"""投票の告知メッセージ、結果メッセージ、投票画面の表示内容を組み立てる。"""

from collections.abc import Sequence

import discord
from discord.utils import escape_markdown, format_dt

from cogs.poll.models import PollRecord, PollStatus

STATUS_COLOURS = {
    PollStatus.OPEN: discord.Colour.green(),
    PollStatus.CLOSED: discord.Colour.orange(),
    PollStatus.FINALIZED: discord.Colour.dark_grey(),
}
# 結果メッセージの色。投票が再開された後の結果は、前回の結果として灰色で示す。
RESULT_COLOURS = {
    PollStatus.OPEN: discord.Colour.light_grey(),
    PollStatus.CLOSED: discord.Colour.gold(),
    PollStatus.FINALIZED: discord.Colour.blue(),
}
# Embedのタイトルと本文の文字数上限。
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
FOOTER = "誰が何に投票したかは、作成者を含め誰にも公開されません。票数は締め切り後に公開されます。"
CHANGEABLE_MODE_TEXT = "変更可(締め切りまで変更・取り消しできます)"
FIXED_MODE_TEXT = "変更不可(投票後の変更・取り消しはできません)"


def build_poll_embed(poll: PollRecord, open_voter_count: int, electorate_count: int, electorate_role_id: int) -> discord.Embed:
    """告知メッセージのEmbedを返す。票数は表示せず、締め切り後は結果メッセージへのリンクを示す。

    Args:
        poll: 表示する投票。
        open_voter_count: 投票受付中に表示する現在の投票者数。締め切り後は保存済みの集計値を使う。
        electorate_count: 作成時点の有権者数。
        electorate_role_id: 投票に必須とする有権者ロールのID。

    """
    status = poll.status
    embed = discord.Embed(title=poll.question, colour=STATUS_COLOURS[status])
    embed.description = "\n".join(_option_line(index, option) for index, option in enumerate(poll.options))
    _add_setting_fields(embed, poll, electorate_role_id)
    voter_count = open_voter_count if status is PollStatus.OPEN else poll.result_voter_count or 0
    embed.add_field(name="投票者数", value=f"{voter_count}人 / 有権者{electorate_count}人")
    embed.add_field(name="状態", value=_announcement_status(poll), inline=False)
    result_url = _result_url(poll)
    if status is PollStatus.OPEN and result_url is not None:
        embed.add_field(name="前回の締め切り時点の結果", value=f"[結果を見る]({result_url})", inline=False)
    embed.set_footer(text=FOOTER)
    return embed


def build_result_embed(poll: PollRecord, electorate_count: int, electorate_role_id: int) -> discord.Embed:
    """締め切り時点の結果を示す、結果メッセージのEmbedを返す。

    投票が再開された後は、前回の締め切り時点の結果であることを示す。
    """
    status = poll.status
    title = "確定結果" if status is PollStatus.FINALIZED else "投票結果"
    embed = discord.Embed(
        title=_truncate(f"{title}: {poll.question}", EMBED_TITLE_LIMIT),
        colour=RESULT_COLOURS[status],
        url=_message_url(poll, poll.message_id),
    )
    counts = poll.result_counts or (0,) * len(poll.options)
    voter_count = poll.result_voter_count or 0
    embed.description = "\n".join(
        f"{_option_line(index, option)} — {_count_text(counts[index], voter_count)}"
        for index, option in enumerate(poll.options)
    )
    _add_setting_fields(embed, poll, electorate_role_id)
    embed.add_field(name="投票者数", value=f"{voter_count}人 / 有権者{electorate_count}人")
    embed.add_field(name="状態", value=_result_status(poll), inline=False)
    embed.set_footer(text=FOOTER)
    return embed


def build_reopen_embed(poll: PollRecord) -> discord.Embed:
    """投票の再開を知らせるEmbedを返す。"""
    embed = discord.Embed(
        title=_truncate(f"投票再開: {poll.question}", EMBED_TITLE_LIMIT),
        colour=STATUS_COLOURS[PollStatus.OPEN],
        url=_message_url(poll, poll.message_id),
    )
    embed.description = (
        f"作成者が投票を再開しました。{format_dt(poll.closes_at, 'f')}({format_dt(poll.closes_at, 'R')})に締め切ります。\n"
        "前回の締め切りまでの票は引き継がれます。告知メッセージの「投票する」から投票してください。"
    )
    return embed


def build_electorate_embed(poll: PollRecord, user_ids: Sequence[int], electorate_role_id: int) -> tuple[discord.Embed, bool]:
    """有権者リストのEmbedと、一覧を添付ファイルで補う必要があるかを返す。

    Embedに収まらない場合は、一覧を省いて件数だけを示す。
    """
    title = _truncate(f"有権者リスト: {poll.question}", EMBED_TITLE_LIMIT)
    embed = discord.Embed(title=title, colour=STATUS_COLOURS[poll.status])
    header = f"{describe_eligibility(poll, electorate_role_id)}: **{len(user_ids)}人**"
    description = f"{header}\n\n{' '.join(f'<@{user_id}>' for user_id in user_ids)}"
    if len(description) <= EMBED_DESCRIPTION_LIMIT:
        embed.description = description
        return embed, False
    embed.description = f"{header}\n\n人数が多いため、全件を添付します。"
    return embed, True


def describe_eligibility(poll: PollRecord, electorate_role_id: int) -> str:
    """投票できる人の条件を返す。対象ロールに加えて、有権者ロールを常に必須とする。"""
    if poll.target_role_id == electorate_role_id:
        return f"作成時点で<@&{electorate_role_id}>を持っていたメンバー"
    return f"作成時点で<@&{poll.target_role_id}>と<@&{electorate_role_id}>の両方を持っていたメンバー"


def describe_choices(options: Sequence[str], choices: Sequence[int] | None) -> str:
    """投票者本人に示す、現在の選択の説明を返す。"""
    if not choices:
        return "まだ投票していません。"
    selected = "、".join(escape_markdown(options[index]) for index in choices)
    return f"現在の選択: **{selected}**"


def _add_setting_fields(embed: discord.Embed, poll: PollRecord, electorate_role_id: int) -> None:
    """告知メッセージと結果メッセージに共通する、投票の設定を追加する。"""
    embed.add_field(name="形式", value="複数選択" if poll.allow_multiple else "単一選択")
    embed.add_field(name="モード", value=CHANGEABLE_MODE_TEXT if poll.allow_change else FIXED_MODE_TEXT)
    embed.add_field(name="作成者", value=f"<@{poll.creator_id}>")
    embed.add_field(name="投票できる人", value=describe_eligibility(poll, electorate_role_id))


def _option_line(index: int, option: str) -> str:
    """番号付きの選択肢1行を返す。"""
    return f"**{index + 1}.** {escape_markdown(option)}"


def _count_text(count: int, voter_count: int) -> str:
    """票数と、投票者数に対する割合を返す。"""
    if voter_count == 0:
        return f"**{count}票**"
    return f"**{count}票**({round(count * 100 / voter_count)}%)"


def _announcement_status(poll: PollRecord) -> str:
    """告知メッセージに示す、進行段階と次に状態が変わる時刻を返す。"""
    result_url = _result_url(poll)
    result_link = f" · [結果を見る]({result_url})" if result_url is not None else ""
    match poll.status:
        case PollStatus.OPEN:
            return f"投票受付中 · {format_dt(poll.closes_at, 'f')}({format_dt(poll.closes_at, 'R')})に締め切り"
        case PollStatus.CLOSED:
            return f"締め切り済み{result_link}"
        case PollStatus.FINALIZED:
            return f"確定{result_link}"


def _result_status(poll: PollRecord) -> str:
    """結果メッセージに示す、結果の扱いを返す。"""
    match poll.status:
        case PollStatus.OPEN:
            return "この結果の後、作成者が投票を再開しました。最新の結果は次の締め切り後に投稿します。"
        case PollStatus.CLOSED:
            finalizes_at = poll.finalizes_at
            if finalizes_at is None:
                return "締め切り済み"
            return f"{format_dt(finalizes_at, 'f')}に確定(それまでは作成者が再開できます)"
        case PollStatus.FINALIZED:
            return "確定"


def _result_url(poll: PollRecord) -> str | None:
    """直近の結果メッセージへのリンクを返す。"""
    return _message_url(poll, poll.result_message_id)


def _message_url(poll: PollRecord, message_id: int | None) -> str | None:
    """投票と同じチャンネルのメッセージへのリンクを返す。"""
    if message_id is None:
        return None
    return f"https://discord.com/channels/{poll.guild_id}/{poll.channel_id}/{message_id}"


def _truncate(text: str, limit: int) -> str:
    """上限を超える文字列を省略記号付きで切り詰める。"""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
