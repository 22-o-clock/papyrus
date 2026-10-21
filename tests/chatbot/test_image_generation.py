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

    def editing_tools(self) -> Any:  # noqa: ANN401 - Discord取得処理のテストダブル。
        """現在の投稿から添付を取得するモックを用意します。"""
        self.source = SimpleNamespace(
            id=5, filename="source.png", content_type="image/png", size=len(PNG), read=AsyncMock(return_value=PNG)
        )
        self.fetch = AsyncMock(return_value=SimpleNamespace(attachments=[self.source]))
        self.edit = AsyncMock(return_value=self.generate.return_value)
        self.client.images.edit = self.edit
        return SimpleNamespace(channel=AsyncMock(return_value=SimpleNamespace(fetch_message=self.fetch)))

    async def test_edits_current_attachment_and_records_image_input(self) -> None:
        """元画像と編集指示を渡し、画像入力料金を記録します。"""
        tools = self.editing_tools()
        self.edit.return_value.usage = SimpleNamespace(
            input_tokens=300, output_tokens=1000, input_tokens_details=SimpleNamespace(image_tokens=200)
        )
        repository = SimpleNamespace(add=AsyncMock())
        state = ReplyConversation()
        arguments = json.dumps(
            {"prompt": "背景を青く", "images": [{"channel_id": "10", "message_id": "20", "attachment_id": "5"}]}
        )
        with patch("cogs.chatbot.observability._usage_repository", repository):
            result = await self.service.edit_tool(state, 21, tools).on_invoke_tool(cast("Any", None), arguments)
        self.assertEqual(json.loads(result)["status"], "ready_to_attach")
        tools.channel.assert_awaited_once_with(10)
        self.fetch.assert_awaited_once_with(20)
        edit_call = self.edit.await_args
        if edit_call is None:
            self.fail("編集APIが呼ばれていません")
        self.assertEqual(edit_call.kwargs["image"], [("source.png", PNG, "image/png")])
        self.assertEqual(edit_call.kwargs["prompt"], "背景を青く")
        self.assertEqual(repository.add.await_args.args[0].image_input_tokens, 200)
        self.assertEqual(state.calls[0]["name"], "edit_image")
        self.assertNotIn(base64.b64encode(PNG).decode(), state.model_dump_json())

    async def test_rejects_unavailable_or_unsupported_sources_before_editing(self) -> None:
        """閲覧不可・削除済み・非画像・サイズ超過でAPIを呼びません。"""
        for scenario in ("forbidden", "missing", "pdf", "oversized"):
            with self.subTest(scenario=scenario):
                tools = self.editing_tools()
                if scenario == "forbidden":
                    tools.channel.side_effect = ValueError("forbidden")
                elif scenario == "missing":
                    self.fetch.return_value.attachments = []
                elif scenario == "pdf":
                    self.source.content_type = "application/pdf"
                else:
                    self.source.size = 50_000_000
                arguments = json.dumps(
                    {"prompt": "test", "images": [{"channel_id": "10", "message_id": "20", "attachment_id": "5"}]}
                )
                result = await self.service.edit_tool(ReplyConversation(), 21, tools).on_invoke_tool(
                    cast("Any", None), arguments
                )
                self.assertIn("error", json.loads(result))
                self.edit.assert_not_awaited()
                self.source.read.assert_not_awaited()

    async def test_invalid_edit_arguments_do_not_download_images(self) -> None:
        tools = self.editing_tools()
        for args in ({}, [], {"prompt": "test", "images": []}, {"prompt": "test", "images": [{}] * 17}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.service.edit(json.dumps(args), tools)
        tools.channel.assert_not_awaited()
        self.edit.assert_not_awaited()
