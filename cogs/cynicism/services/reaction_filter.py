"""🥶を冷笑ポイントの対象として認識するための判定。"""

import re

import discord

from cogs.cynicism.constants import CUSTOM_CYNICISM_EMOJI_NAME, CYNICISM_EMOJI, VARIATION_SELECTOR

# 本文中のカスタム絵文字タグ(`<:name:id>` / `<a:name:id>`)から対象のものだけを取り除くための正規表現。
_CUSTOM_CYNICISM_EMOJI_TAG = re.compile(rf"<a?:{re.escape(CUSTOM_CYNICISM_EMOJI_NAME)}:\d+>")


def is_cynicism_emoji(emoji: discord.PartialEmoji) -> bool:
    """Unicodeの🥶と、対象のカスタム絵文字を対象とする。"""
    if emoji.id is None:
        return emoji.name == CYNICISM_EMOJI
    return emoji.name == CUSTOM_CYNICISM_EMOJI_NAME


def is_cynicism_only_content(content: str) -> bool:
    """空白・異体字セレクタ・対象カスタム絵文字のタグを除いた本文が、🥶だけで構成されるかを返す。"""
    without_custom_emoji, custom_emoji_count = _CUSTOM_CYNICISM_EMOJI_TAG.subn("", content)
    normalized = "".join(without_custom_emoji.replace(VARIATION_SELECTOR, "").split())
    if not normalized:
        return custom_emoji_count > 0
    return set(normalized) == {CYNICISM_EMOJI}
