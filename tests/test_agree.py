import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

from cogs.agree.agree import Agree

if TYPE_CHECKING:
    from discord import Interaction, Message
    from discord.ext import commands


class AgreeMemoryExclusionTest(unittest.IsolatedAsyncioTestCase):
    async def test_agree_marks_generated_message_as_excluded(self) -> None:
        sent_message = SimpleNamespace(id=300)
        interaction = cast(
            "Interaction",
            SimpleNamespace(
                response=SimpleNamespace(send_message=AsyncMock()),
                original_response=AsyncMock(return_value=sent_message),
            ),
        )
        source = cast("Message", SimpleNamespace(content="同意します", jump_url="https://example.com/messages/1"))
        bot = Mock()
        cog = Agree(cast("commands.Bot[Any]", bot))

        await cog.agree(interaction, source)

        bot.dispatch.assert_called_once_with("exclude_from_long_term_memory", sent_message)

    async def test_disagree_marks_generated_message_as_excluded(self) -> None:
        sent_message = SimpleNamespace(id=301)
        interaction = cast(
            "Interaction",
            SimpleNamespace(
                response=SimpleNamespace(send_message=AsyncMock()),
                original_response=AsyncMock(return_value=sent_message),
            ),
        )
        source = cast("Message", SimpleNamespace(content="同意します"))
        bot = Mock()
        cog = Agree(cast("commands.Bot[Any]", bot))

        await cog.disagree(interaction, source)

        bot.dispatch.assert_called_once_with("exclude_from_long_term_memory", sent_message)
