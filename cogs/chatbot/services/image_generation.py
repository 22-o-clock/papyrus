import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord
from agents import FunctionTool
from agents.tool_context import ToolContext
from openai import APIError, AsyncOpenAI
from openai.types import ImagesResponse

from cogs.chatbot.models.reply_conversation import ReplyConversation
from cogs.chatbot.observability import observe_chatbot_api_call
from cogs.chatbot.services.conversation_tools import ConversationTools

IMAGE_MODEL = "gpt-image-2.5-flare"
MAX_SOURCE_IMAGES = 16
MAX_SOURCE_BYTES = 50_000_000


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """今回の回答に添付する画像。永続化する会話にはバイナリを含めません。"""

    filename: str
    content: bytes


class ImageGeneration:
    """1回答内で生成した画像を保持し、生成費用を計測します。"""

    def __init__(self, client: AsyncOpenAI, maximum_file_size: int) -> None:
        """APIクライアントとDiscordの添付上限を設定します。"""
        self.client = client
        self.maximum_file_size = maximum_file_size
        self.images: list[GeneratedImage] = []

    async def generate(self, arguments: str) -> str:
        """文章の指示から画像を1枚生成し、添付用に保持します。

        Args:
            arguments: promptを含むJSONオブジェクト。

        Returns:
            生成した画像のファイル名と、回答時に添付することを示すJSON。

        Raises:
            ValueError: 指示・画像データが不正、または添付上限を超える場合。
            APIError: 画像生成APIが失敗した場合。

        """
        args = json.loads(arguments)
        prompt = args.get("prompt") if isinstance(args, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            error_message = "画像の生成指示を指定してください。"
            raise ValueError(error_message)
        response = await observe_chatbot_api_call(
            "image_generation",
            IMAGE_MODEL,
            self.client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                n=1,
                size="1024x1024",
                quality="medium",
                output_format="png",
            ),
        )
        return self._store_image(response)

    async def edit(self, arguments: str, tools: ConversationTools) -> str:
        """閲覧可能なDiscord添付を編集し、結果を返信用に保持します。"""
        args = json.loads(arguments)
        prompt = args.get("prompt") if isinstance(args, dict) else None
        sources = args.get("images") if isinstance(args, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            error_message = "編集指示を指定してください。"
            raise ValueError(error_message)
        if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCE_IMAGES:
            error_message = "編集元の画像を1〜16枚指定してください。"
            raise ValueError(error_message)
        inputs: list[tuple[str, bytes, str]] = []
        for source in sources:
            channel = await tools.channel(int(source["channel_id"]))
            message = await channel.fetch_message(int(source["message_id"]))
            attachment = next((a for a in message.attachments if a.id == int(source["attachment_id"])), None)
            if attachment is None:
                error_message = "指定した画像が投稿に存在しません。"
                raise ValueError(error_message)
            if attachment.content_type not in {"image/png", "image/jpeg", "image/webp"}:
                error_message = "編集元にはPNG・JPEG・WebP画像を指定してください。"
                raise ValueError(error_message)
            if attachment.size >= MAX_SOURCE_BYTES:
                error_message = "編集元の画像は1枚50MB未満にしてください。"
                raise ValueError(error_message)
            content = await attachment.read()
            if len(content) >= MAX_SOURCE_BYTES:
                error_message = "編集元の画像は1枚50MB未満にしてください。"
                raise ValueError(error_message)
            inputs.append((attachment.filename, content, attachment.content_type))
        response = await observe_chatbot_api_call(
            "image_generation",
            IMAGE_MODEL,
            self.client.images.edit(
                model=IMAGE_MODEL,
                image=inputs,
                prompt=prompt,
                n=1,
                size="1024x1024",
                quality="medium",
                output_format="png",
            ),
        )
        return self._store_image(response)

    def _store_image(self, response: ImagesResponse) -> str:
        """生成・編集結果を検証し、回答への添付用に保持します。"""
        if not response.data or not response.data[0].b64_json:
            error_message = "画像生成APIから画像が返りませんでした。"
            raise ValueError(error_message)
        try:
            content = base64.b64decode(response.data[0].b64_json, validate=True)
        except binascii.Error as exc:
            error_message = "画像データを読み取れませんでした。"
            raise ValueError(error_message) from exc
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            error_message = "生成結果がPNG画像ではありません。"
            raise ValueError(error_message)
        if len(content) > self.maximum_file_size:
            error_message = "生成画像がDiscordの添付サイズ上限を超えました。"
            raise ValueError(error_message)
        filename = f"generated-{len(self.images) + 1}.png"
        self.images.append(GeneratedImage(filename, content))
        return json.dumps({"filename": filename, "status": "ready_to_attach"})

    def tool(self, state: ReplyConversation, message_id: int) -> FunctionTool:
        """生成の判断をモデルに委ねる関数ツールを返します。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "構図・被写体・画風・画像内の文字などの生成指示。"}},
            "required": ["prompt"],
            "additionalProperties": False,
        }
        return self._tool(
            state,
            message_id,
            self.generate,
            name="generate_image",
            description="ユーザーが画像の作成を求めた場合に、文章の指示から新しい画像を1枚生成します。"
            "画像生成についての質問だけでは使いません。元画像を使う変更にはedit_imageを使います。"
            "成功した画像は回答に自動添付されるため、架空の画像URLやMarkdown画像リンクは不要です。",
            params_json_schema=schema,
        )

    def edit_tool(self, state: ReplyConversation, message_id: int, tools: ConversationTools) -> FunctionTool:
        """Discord添付の識別子で編集対象を指定するツールを返します。"""

        async def edit(arguments: str) -> str:
            """現在のサーバーで編集対象を取得します。"""
            return await self.edit(arguments, tools)

        reference = {
            "type": "object",
            "properties": {key: {"type": "string"} for key in ("channel_id", "message_id", "attachment_id")},
            "required": ["channel_id", "message_id", "attachment_id"],
            "additionalProperties": False,
        }
        return self._tool(
            state,
            message_id,
            edit,
            name="edit_image",
            description="添付画像や以前生成した画像を、ユーザーの指示に従って編集・合成します。"
            "返信先の画像はget_messageで投稿と添付のIDを確認してください。"
            "元の被写体・構図を維持する要望もpromptに含めます。結果は回答に自動添付されます。",
            params_json_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "変更内容と維持したい部分。"},
                    "images": {"type": "array", "items": reference, "minItems": 1, "maxItems": MAX_SOURCE_IMAGES},
                },
                "required": ["prompt", "images"],
                "additionalProperties": False,
            },
        )

    def _tool(  # noqa: PLR0913 - 共通の実行処理とツールの表示・スキーマを受け取る。
        self,
        state: ReplyConversation,
        message_id: int,
        action: Callable[[str], Awaitable[str]],
        *,
        name: str,
        description: str,
        params_json_schema: dict[str, Any],
    ) -> FunctionTool:
        """画像処理の成功・失敗を記録するツールを構築します。"""

        async def invoke(_context: ToolContext, arguments: str) -> str:
            """生成失敗をモデルへ返し、画像本体を除いた呼び出し記録を保存します。"""
            try:
                result = await action(arguments)
                status = "completed"
            except APIError:
                result = json.dumps({"error": "画像生成APIが失敗しました。画像は生成できていません。"}, ensure_ascii=False)
                status = "failed"
            except (ValueError, TypeError, KeyError, discord.HTTPException) as exc:
                result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                status = "failed"
            state.calls.append(
                {"name": name, "arguments": arguments, "status": status, "requested_by_message_id": str(message_id)}
            )
            return result

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=params_json_schema,
            on_invoke_tool=invoke,
        )
