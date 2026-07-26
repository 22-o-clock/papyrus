"""🥶を冷笑ポイントの対象として認識するための判定。"""

import discord

from cogs.cynicism.constants import CYNICISM_EMOJI, VARIATION_SELECTOR


def is_cynicism_emoji(emoji: discord.PartialEmoji) -> bool:
    """カスタム絵文字を除外し、Unicodeの🥶だけを対象とする。"""
    return emoji.id is None and emoji.name == CYNICISM_EMOJI


def is_cynicism_only_content(content: str) -> bool:
    """空白と異体字セレクタを除いた本文が🥶だけで構成されるかを返す。"""
    normalized = "".join(content.replace(VARIATION_SELECTOR, "").split())
    if not normalized:
        return False
    return set(normalized) == {CYNICISM_EMOJI}
