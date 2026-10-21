import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext
from openai import APIError, AsyncOpenAI

from cogs.chatbot.models.reply_conversation import ReplyConversation
from cogs.chatbot.observability import observe_chatbot_api_call

IMAGE_MODEL = "gpt-image-2.5-flare"


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

        async def invoke(_context: ToolContext, arguments: str) -> str:
            """生成失敗をモデルへ返し、画像本体を除いた呼び出し記録を保存します。"""
            try:
                result = await self.generate(arguments)
                status = "completed"
            except APIError:
                result = json.dumps({"error": "画像生成APIが失敗しました。画像は生成できていません。"}, ensure_ascii=False)
                status = "failed"
            except (ValueError, TypeError) as exc:
                result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                status = "failed"
            state.calls.append(
                {"name": "generate_image", "arguments": arguments, "status": status, "requested_by_message_id": str(message_id)}
            )
            return result

        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "構図・被写体・画風・画像内の文字などの生成指示。"}},
            "required": ["prompt"],
            "additionalProperties": False,
        }
        return FunctionTool(
            name="generate_image",
            description="ユーザーが画像の作成を求めた場合に、文章の指示から新しい画像を1枚生成します。"
            "画像生成についての質問だけでは使いません。元画像の編集には対応しません。"
            "成功した画像は回答に自動添付されるため、架空の画像URLやMarkdown画像リンクは不要です。",
            params_json_schema=schema,
            on_invoke_tool=invoke,
        )
