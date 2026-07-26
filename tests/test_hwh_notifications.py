import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.hwh.hwh import Patchwork


class EventNotificationMemoryExclusionTest(unittest.IsolatedAsyncioTestCase):
    @patch("cogs.hwh.hwh._fetch_text_channel", new_callable=AsyncMock)
    async def test_marks_all_event_notifications_as_excluded(self, fetch_text_channel: AsyncMock) -> None:
        sent_messages = [SimpleNamespace(id=101), SimpleNamespace(id=102), SimpleNamespace(id=103)]
        channel = SimpleNamespace(send=AsyncMock(side_effect=sent_messages))
        fetch_text_channel.return_value = channel
        bot = Mock()
        cog = object.__new__(Patchwork)
        cog.bot = bot
        cog.runtime_environment = SimpleNamespace(is_debug=False)
        cog.event_notify_channel = 10
        cog.__dict__["_create_event_embed"] = AsyncMock(return_value=discord.Embed())
        start_time = datetime.datetime(2026, 7, 26, tzinfo=datetime.UTC)
        created = SimpleNamespace(name="イベント", status=discord.EventStatus.scheduled)
        before = SimpleNamespace(
            name="イベント",
            description="説明",
            entity_type=discord.EntityType.external,
            channel_id=None,
            location="会場",
            start_time=start_time,
            status=discord.EventStatus.scheduled,
        )
        after = SimpleNamespace(
            name="イベント",
            description="説明",
            entity_type=discord.EntityType.external,
            channel_id=None,
            location="会場",
            start_time=start_time,
            status=discord.EventStatus.active,
        )

        await cog.event_create_notify(created)
        await cog.event_update_notify(before, after)
        await cog.event_delete_notify(created)

        if [call.args for call in bot.dispatch.call_args_list] != [
            ("exclude_from_long_term_memory", sent_message) for sent_message in sent_messages
        ]:
            self.fail("予定イベント通知へ長期記憶の除外フラグを付けていません")
