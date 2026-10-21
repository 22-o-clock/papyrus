import io
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import discord

from cogs.chatbot.constants import DISCORD_RESPONSE_CHUNK_LENGTH
from cogs.chatbot.services.message_delivery import reply_with_split_response


class MessageDeliveryTest(IsolatedAsyncioTestCase):
    async def test_batches_text_and_files_without_empty_or_duplicate_messages(self) -> None:
        """文字数・添付数の境界と画像のみの回答を確認します。"""
        for content, count, expected in (
            ("caption", 1, [("caption", 1)]),
            ("", 1, [(None, 1)]),
            ("caption", 11, [("caption", 10), (None, 1)]),
            ("a" * (DISCORD_RESPONSE_CHUNK_LENGTH + 1), 1, [("a" * DISCORD_RESPONSE_CHUNK_LENGTH, 1), ("a", 0)]),
            ("", 0, []),
        ):
            with self.subTest(length=len(content), images=count):
                target = SimpleNamespace(reply=AsyncMock())
                second = SimpleNamespace(reply=AsyncMock(return_value=SimpleNamespace(id=2)))
                target.reply.return_value = second
                files = [discord.File(io.BytesIO(b"image"), filename=f"{index}.png") for index in range(count)]
                try:
                    sent = await reply_with_split_response(cast("Any", target), content, files=files)
                    calls = target.reply.await_args_list + second.reply.await_args_list
                    actual = [(call.args[0], len(call.kwargs["files"])) for call in calls]
                    if actual != expected or len(sent) != len(expected):
                        self.fail("送信内容または分割数が一致しません")
                    delivered = [file for call in calls for file in call.kwargs["files"]]
                    if delivered != files:
                        self.fail("添付の順序が変わったか重複・欠落があります")
                finally:
                    for file in files:
                        file.close()
