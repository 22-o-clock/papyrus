import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import discord
from discord import Message

from cogs.chatbot.responses_api import ReactionInMemory, ReactionUserInMemory
from cogs.chatbot.services.reaction_context import (
    REACTION_REACTOR_LIMIT,
    collect_message_reactions,
    preserve_known_reactors,
    resolve_reaction_emoji,
)

CUSTOM_EMOJI_ID = 123


class FakeReaction:
    """通常・スーパーリアクションの利用者を返すテスト用オブジェクト。"""

    def __init__(self) -> None:
        self.normal_count = REACTION_REACTOR_LIMIT + 5
        self.burst_count = 1
        self.emoji = discord.PartialEmoji(name="party", id=CUSTOM_EMOJI_ID, animated=True)
        self.message = SimpleNamespace(id=999)

    async def users(
        self,
        *,
        limit: int,
        **kwargs: object,
    ) -> AsyncIterator[SimpleNamespace]:
        """リアクション種別に応じた上限以内の利用者を返します。"""
        count = limit if kwargs["type"] is discord.ReactionType.normal else 1
        for user_id in range(1, count + 1):
            yield SimpleNamespace(id=user_id, display_name=f"利用者{user_id}", bot=user_id == 1)


class FakeGatewayUnicodeReaction(FakeReaction):
    """種別別件数がないGateway由来のUnicodeリアクション。"""

    def __init__(self) -> None:
        super().__init__()
        self.normal_count = 0
        self.burst_count = 0
        self.count = 1
        self.emoji = "👍"


class ReactionContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_collects_custom_normal_and_burst_reactions_separately(self) -> None:
        message = cast("Message", SimpleNamespace(reactions=[cast("discord.Reaction", FakeReaction())]))

        reactions = await collect_message_reactions(message)

        if [reaction.reaction_type for reaction in reactions] != ["normal", "burst"]:
            self.fail("通常リアクションとスーパーリアクションが分離されていません")
        normal, burst = reactions
        if normal.count != REACTION_REACTOR_LIMIT + 5 or len(normal.reactors) != REACTION_REACTOR_LIMIT:
            self.fail("通常リアクションの総数または人物上限が保持されていません")
        if not normal.reactors_truncated or normal.reactors_incomplete:
            self.fail("上限による省略と取得失敗が区別されていません")
        if normal.emoji_id != CUSTOM_EMOJI_ID or normal.emoji_name != "party" or not normal.animated:
            self.fail("カスタム絵文字の識別情報が保持されていません")
        if burst.count != 1 or burst.reactors[0].user_id != 1 or not burst.reactors[0].is_bot:
            self.fail("スーパーリアクションの人物情報が保持されていません")

    async def test_preserves_unicode_total_when_gateway_has_no_type_counts(self) -> None:
        message = cast("Message", SimpleNamespace(reactions=[cast("discord.Reaction", FakeGatewayUnicodeReaction())]))

        reactions = await collect_message_reactions(message)

        if len(reactions) != 1 or reactions[0].emoji_name != "👍" or reactions[0].count != 1:
            self.fail("Gateway由来のUnicodeリアクション総数を保持できていません")

    def test_resolves_unicode_emoji_as_is(self) -> None:
        guild = cast("discord.Guild", SimpleNamespace(emojis=[]))

        resolved = resolve_reaction_emoji(guild, "👍")

        if resolved != "👍":
            self.fail("Unicode絵文字がそのまま解決されていません")

    def test_resolves_custom_emoji_mention_to_guild_emoji(self) -> None:
        snowflake_id = 123456789012345678
        party_emoji = SimpleNamespace(id=snowflake_id, name="party", animated=True)
        guild = cast("discord.Guild", SimpleNamespace(emojis=[party_emoji]))

        resolved = resolve_reaction_emoji(guild, f"<a:party:{snowflake_id}>")

        if resolved is not party_emoji:
            self.fail("メンション表記からサーバーのカスタム絵文字が解決されていません")

    def test_resolves_bare_custom_emoji_name_to_guild_emoji(self) -> None:
        party_emoji = SimpleNamespace(id=123456789012345678, name="party", animated=True)
        guild = cast("discord.Guild", SimpleNamespace(emojis=[party_emoji]))

        resolved = resolve_reaction_emoji(guild, ":party:")

        if resolved is not party_emoji:
            self.fail("ベア名表記からサーバーのカスタム絵文字が解決されていません")

    def test_falls_back_to_partial_emoji_when_mentioned_id_is_unknown(self) -> None:
        guild = cast("discord.Guild", SimpleNamespace(emojis=[]))
        unknown_id = 987654321098765432

        resolved = resolve_reaction_emoji(guild, f"<:ghost:{unknown_id}>")

        if not isinstance(resolved, discord.PartialEmoji) or resolved.id != unknown_id:
            self.fail("未知のカスタム絵文字IDがPartialEmojiへ解決されていません")

    def test_falls_back_to_raw_text_when_bare_name_is_unknown(self) -> None:
        guild = cast("discord.Guild", SimpleNamespace(emojis=[]))

        resolved = resolve_reaction_emoji(guild, "unknown_emoji")

        if resolved != "unknown_emoji":
            self.fail("未知のベア名がそのまま返されていません")

    def test_preserves_known_users_when_reactor_fetch_is_incomplete(self) -> None:
        previous_reactor = ReactionUserInMemory(user_id=10, display_name="既知の利用者", is_bot=False)
        previous = ReactionInMemory(
            emoji_name="👍",
            emoji_id=None,
            animated=False,
            reaction_type="normal",
            count=1,
            reactors=[previous_reactor],
        )
        incomplete = ReactionInMemory(
            emoji_name="👍",
            emoji_id=None,
            animated=False,
            reaction_type="normal",
            count=1,
            reactors_incomplete=True,
        )

        merged = preserve_known_reactors([incomplete], [previous])

        if merged[0].reactors != [previous_reactor] or not merged[0].reactors_incomplete:
            self.fail("人物取得失敗時に既知の利用者と不完全状態を保持できていません")
