"""ランキング表示に使うメンバー情報の解決。"""

from collections.abc import Mapping, Sequence

import discord
from discord.ext import commands

from cogs.cynicism.models import RankedMemberIdentity


def resolve_identities(
    bot: commands.Bot,
    guild: discord.Guild | None,
    member_ids: Sequence[int],
    stored_names: Mapping[int, str],
) -> dict[int, RankedMemberIdentity]:
    """サーバー、Botキャッシュ、TalkDataの順に表示名とBot判定を解決する。"""
    identities: dict[int, RankedMemberIdentity] = {}
    for member_id in member_ids:
        member = guild.get_member(member_id) if guild is not None else None
        if member is not None:
            identities[member_id] = RankedMemberIdentity(member_id, member.display_name, is_bot=member.bot)
            continue
        user = bot.get_user(member_id)
        if user is not None:
            identities[member_id] = RankedMemberIdentity(member_id, user.display_name, is_bot=user.bot)
            continue
        # 退出済みなどで解決できない場合は、TalkDataに残る表示名で代替する。
        identities[member_id] = RankedMemberIdentity(
            member_id,
            stored_names.get(member_id, str(member_id)),
            is_bot=False,
        )
    return identities
