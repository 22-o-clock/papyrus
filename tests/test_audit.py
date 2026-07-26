import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import Attachment, Message, TextChannel

from cogs.audit.audit import (
    UNAVAILABLE_REPLY_PREFIX,
    Audit,
    Event,
    _create_reply_prefix,
    _has_auditable_edit,
    create_webhook_log_message,
)

if TYPE_CHECKING:
    from discord.ext import commands


def _not_found() -> discord.NotFound:
    response = Mock(status=404, reason="Not Found", headers={})
    return discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})


class AuditableEditTest(unittest.TestCase):
    def test_detects_attachment_addition_without_content_change(self) -> None:
        before = cast("Message", SimpleNamespace(content="same", attachments=[]))
        after = cast("Message", SimpleNamespace(content="same", attachments=[object()]))

        if not _has_auditable_edit(before, after):
            raise AssertionError

    def test_ignores_non_content_update_when_attachments_are_unchanged(self) -> None:
        attachment = object()
        before = cast("Message", SimpleNamespace(content="same", attachments=[attachment]))
        after = cast("Message", SimpleNamespace(content="same", attachments=[attachment]))

        if _has_auditable_edit(before, after):
            raise AssertionError


class AuditLogMessageTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(*, attachments: list[Attachment], fetch_message: AsyncMock | None = None) -> Message:
        channel = SimpleNamespace(name="general", fetch_message=fetch_message or AsyncMock())
        return cast(
            "Message",
            SimpleNamespace(
                attachments=attachments,
                author=SimpleNamespace(display_name="member", display_avatar=SimpleNamespace(url="https://example.com/avatar")),
                channel=channel,
                content="message body",
                created_at=datetime(2026, 7, 26, tzinfo=UTC),
                edited_at=None,
                embeds=[],
                guild=None,
                interaction_metadata=None,
                jump_url="https://discord.com/channels/1/2/3",
                reference=None,
                stickers=[],
            ),
        )

    async def test_continues_when_referenced_message_is_unavailable(self) -> None:
        fetch_message = AsyncMock(side_effect=_not_found())
        message = self._message(attachments=[], fetch_message=fetch_message)
        message.reference = SimpleNamespace(message_id=456)

        prefix = await _create_reply_prefix(message)

        if prefix != UNAVAILABLE_REPLY_PREFIX:
            raise AssertionError(prefix)
        fetch_message.assert_awaited_once_with(456)

    async def test_uses_cached_attachment_and_keeps_log_when_one_is_unavailable(self) -> None:
        available_file = Mock()
        available_to_file = AsyncMock(return_value=available_file)
        available = cast(
            "Attachment",
            SimpleNamespace(
                id=1,
                filename="available.txt",
                to_file=available_to_file,
            ),
        )
        unavailable_to_file = AsyncMock(side_effect=_not_found())
        unavailable = cast(
            "Attachment",
            SimpleNamespace(
                id=2,
                filename="missing.txt",
                to_file=unavailable_to_file,
            ),
        )
        message = self._message(attachments=[available, unavailable])

        payload = await create_webhook_log_message(message, Event.delete)

        if payload["files"] != [available_file]:
            raise AssertionError(payload["files"])
        if "Attachment unavailable: missing.txt" not in payload["content"]:
            raise AssertionError(payload["content"])
        available_to_file.assert_awaited_once_with(use_cached=True)
        unavailable_to_file.assert_awaited_once_with(use_cached=True)


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
