from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

import discord
from discord import Attachment, Message

from cogs.audit.audit import (
    UNAVAILABLE_REPLY_PREFIX,
    Event,
    _create_reply_prefix,
    _has_auditable_edit,
    create_webhook_log_message,
)


def _not_found() -> discord.NotFound:
    response = Mock(status=404, reason="Not Found", headers={})
    return discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})


class AuditableEditTest(TestCase):
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


class AuditLogMessageTest(IsolatedAsyncioTestCase):
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
