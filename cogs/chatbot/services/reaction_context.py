from logging import getLogger
from typing import Literal

import discord
from discord import Message

from cogs.chatbot.responses_api import ReactionInMemory, ReactionUserInMemory

logger = getLogger(__name__)

REACTION_REACTOR_LIMIT = 20


async def collect_message_reactions(message: Message) -> list[ReactionInMemory]:
    """Discordメッセージから発言生成用のリアクション一覧を取得します。"""
    collected: list[ReactionInMemory] = []
    for reaction in message.reactions:
        normal_count = int(reaction.normal_count or 0)
        burst_count = int(reaction.burst_count or 0)
        if normal_count + burst_count == 0 and reaction.count:
            # Gateway由来のReactionには種別別件数がない場合があるため、総数を通常扱いで失わず保持する。
            normal_count = reaction.count
        if normal_count:
            collected.append(await _collect_reaction(reaction, "normal", normal_count))
        if burst_count:
            collected.append(await _collect_reaction(reaction, "burst", burst_count))
    return collected


def resolve_reaction_emoji(guild: discord.Guild | None, raw_emoji: str) -> str | discord.Emoji | discord.PartialEmoji:
    """LLMが生成した絵文字表記を、Discordへリアクション可能な形式へ解決します。

    `<a:name:id>` 形式やベア名 (`name`, `:name:`) をサーバーのカスタム絵文字と突き合わせ、
    一致すればそのEmoji/PartialEmojiを、一致しなければ入力をUnicode絵文字とみなしそのまま返します。
    """
    candidate = discord.PartialEmoji.from_str(raw_emoji.strip())
    if guild is None:
        return candidate if candidate.id is not None else raw_emoji

    if candidate.id is not None:
        resolved = discord.utils.get(guild.emojis, id=candidate.id)
        return resolved if resolved is not None else candidate

    bare_name = candidate.name.strip(":")
    resolved = discord.utils.get(guild.emojis, name=bare_name)
    return resolved if resolved is not None else raw_emoji


def preserve_known_reactors(
    reactions: list[ReactionInMemory],
    previous_reactions: list[ReactionInMemory],
) -> list[ReactionInMemory]:
    """人物取得が不完全なリアクションへ、直前まで判明していた利用者を補います。"""
    previous_by_key = {
        (reaction.emoji_id, reaction.emoji_name, reaction.reaction_type): reaction for reaction in previous_reactions
    }
    for reaction in reactions:
        if not reaction.reactors_incomplete:
            continue
        previous = previous_by_key.get((reaction.emoji_id, reaction.emoji_name, reaction.reaction_type))
        if previous is None:
            continue
        known_by_user_id = {reactor.user_id: reactor for reactor in reaction.reactors}
        for reactor in previous.reactors:
            known_by_user_id.setdefault(reactor.user_id, reactor)
        maximum_known_reactors = min(reaction.count, REACTION_REACTOR_LIMIT)
        reaction.reactors = list(known_by_user_id.values())[:maximum_known_reactors]
    return reactions


async def _collect_reaction(
    reaction: discord.Reaction,
    reaction_type: Literal["normal", "burst"],
    count: int,
) -> ReactionInMemory:
    """同じ絵文字の通常・スーパーリアクションを別々に取得します。"""
    discord_reaction_type = discord.ReactionType.normal if reaction_type == "normal" else discord.ReactionType.burst
    reactors: list[ReactionUserInMemory] = []
    request_failed = False
    try:
        reactors.extend(
            [
                ReactionUserInMemory(
                    user_id=user.id,
                    display_name=user.display_name,
                    is_bot=user.bot,
                )
                async for user in reaction.users(
                    limit=REACTION_REACTOR_LIMIT,
                    type=discord_reaction_type,
                )
            ]
        )
    except discord.HTTPException:
        request_failed = True
        logger.warning(
            "Failed to fetch chatbot reaction users (message_id=%s, emoji=%s, reaction_type=%s)",
            reaction.message.id,
            reaction.emoji,
            reaction_type,
            exc_info=True,
        )

    emoji = reaction.emoji
    if isinstance(emoji, str):
        emoji_name = emoji
        emoji_id = None
        animated = False
    else:
        emoji_name = emoji.name or str(emoji)
        emoji_id = emoji.id
        animated = bool(emoji.animated)

    expected_reactors = min(count, REACTION_REACTOR_LIMIT)
    return ReactionInMemory(
        emoji_name=emoji_name,
        emoji_id=emoji_id,
        animated=animated,
        reaction_type=reaction_type,
        count=count,
        reactors=reactors,
        reactors_truncated=count > REACTION_REACTOR_LIMIT,
        reactors_incomplete=request_failed or len(reactors) < expected_reactors,
    )
