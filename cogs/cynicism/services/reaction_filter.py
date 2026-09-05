"""🥶を冷笑ポイントの対象として認識するための判定。"""

import discord

from cogs.cynicism.constants import CUSTOM_CYNICISM_EMOJI_NAME, CYNICISM_EMOJI


def is_cynicism_emoji(emoji: discord.PartialEmoji) -> bool:
    """Unicodeの🥶と、対象のカスタム絵文字を対象とする。"""
    if emoji.id is None:
        return emoji.name == CYNICISM_EMOJI
    return emoji.name == CUSTOM_CYNICISM_EMOJI_NAME
