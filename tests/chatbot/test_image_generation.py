# ruff: noqa: PT009, PT027 -- unittestの標準アサーションを使用。
import base64
import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from cogs.chatbot.models.reply_conversation import ReplyConversation
from cogs.chatbot.services.image_generation import ImageGeneration

PNG = b"\x89PNG\r\n\x1a\n" + b"test image"


class ImageGenerationTest(unittest.IsolatedAsyncioTestCase):
    """画像の保持、実測usage、失敗時の応答を検証します。"""

    def setUp(self) -> None:
        self.generate = AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())],
                usage=SimpleNamespace(input_tokens=100, output_tokens=1000, total_tokens=1100),
            )
        )
        self.client = SimpleNamespace(images=SimpleNamespace(generate=self.generate))
        self.service = ImageGeneration(cast("Any", self.client), 1024)

    async def test_records_cost_and_keeps_binary_out_of_conversation(self) -> None:
        repository = SimpleNamespace(add=AsyncMock())
        state = ReplyConversation()
        with patch("cogs.chatbot.observability._usage_repository", repository):
            result = await self.service.tool(state, 1).on_invoke_tool(cast("Any", None), '{"prompt":"猫を描いて"}')
        self.assertEqual(json.loads(result)["status"], "ready_to_attach")
        self.assertEqual(self.service.images[0].content, PNG)
        self.assertNotIn(base64.b64encode(PNG).decode(), state.model_dump_json())
        increment = repository.add.await_args.args[0]
        self.assertEqual(increment.operation, "image_generation")
        self.assertEqual(increment.model, "gpt-image-2.5-flare")
        self.assertEqual((increment.input_tokens, increment.output_tokens), (100, 1000))

    async def test_invalid_prompt_does_not_generate(self) -> None:
        for arguments in ("{}", "[]", '{"prompt":" "}'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.service.generate(arguments)
        self.generate.assert_not_awaited()

    async def test_invalid_or_oversized_image_is_not_attached(self) -> None:
        for content in ("!invalid!", base64.b64encode(b"not png").decode(), base64.b64encode(PNG * 100).decode(), None):
            self.generate.return_value.data = [SimpleNamespace(b64_json=content)]
            with self.subTest(content=content), self.assertRaises(ValueError):
                await self.service.generate('{"prompt":"test"}')
        self.assertEqual(self.service.images, [])

    async def test_failure_is_returned_to_agent(self) -> None:
        self.generate.return_value.data = []
        state = ReplyConversation()
        result = await self.service.tool(state, 1).on_invoke_tool(cast("Any", None), '{"prompt":"test"}')
        self.assertIn("error", json.loads(result))
        self.assertEqual(state.calls[0]["status"], "failed")

    async def test_images_are_scoped_to_one_reply(self) -> None:
        other = ImageGeneration(cast("Any", self.client), 1024)
        await self.service.generate('{"prompt":"test"}')
        self.assertEqual(other.images, [])
