import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

from discord import TextChannel

from cogs.audit.audit import Audit

if TYPE_CHECKING:
    import discord
    from discord import Message
    from discord.ext import commands


class AuditLogMemoryExclusionTest(unittest.IsolatedAsyncioTestCase):
    @patch("cogs.audit.audit.create_webhook_log_message", new_callable=AsyncMock)
    @patch("cogs.audit.audit._get_or_fetch_channel", new_callable=AsyncMock)
    async def test_marks_webhook_log_as_excluded(
        self,
        get_or_fetch_channel: AsyncMock,
        create_webhook_log_message: AsyncMock,
    ) -> None:
        sent_message = SimpleNamespace(id=500)
        hook = SimpleNamespace(id=900, send=AsyncMock(return_value=sent_message))
        channel = Mock(spec=TextChannel)
        channel.id = 20
        message = cast(
            "Message",
            SimpleNamespace(
                guild=SimpleNamespace(id=1),
                channel=channel,
                webhook_id=None,
            ),
        )
        create_webhook_log_message.return_value = {"content": "監査ログ"}
        get_or_fetch_channel.return_value = SimpleNamespace(id=30)
        bot = Mock()
        cog = object.__new__(Audit)
        cog.bot = cast("commands.Bot", bot)
        cog.runtime_environment = SimpleNamespace(is_debug=False)
        cog.server_id = 1
        cog.log_thread = 30
        cog.audit_immunity = []
        cog.hook = cast("discord.Webhook", hook)

        await cog.message_delete_log(message)

        hook.send.assert_awaited_once_with(content="監査ログ", thread=get_or_fetch_channel.return_value, wait=True)
        bot.dispatch.assert_called_once_with("exclude_from_long_term_memory", sent_message)
