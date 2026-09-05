import json
from typing import Any

import discord
from agents import CodeInterpreterTool, FunctionTool, OpenAIResponsesModel, Tool, WebSearchTool
from agents.items import ModelResponse
from agents.tool import ToolOutputFileContent, ToolOutputImage
from agents.tool_context import ToolContext
from openai import AsyncOpenAI

from cogs.chatbot.models.reply_conversation import ReplyConversation
from cogs.chatbot.observability import observe_chatbot_api_call
from cogs.chatbot.services.conversation_tools import FUNCTION_TOOLS, ConversationTools

COMPACT_THRESHOLD = 30_000


class ObservedResponsesModel(OpenAIResponsesModel):
    """SDK経由の各API呼び出しを既存の日次利用量集計へ記録します。"""

    def __init__(self, state: ReplyConversation, client: AsyncOpenAI) -> None:
        """会話状態とAPIクライアントを設定します。"""
        super().__init__(model=state.model, openai_client=client)
        self.state = state
        self.completed_responses = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:  # noqa: ANN401 - SDKの呼び出し引数をそのまま転送。
        """生成を計測し、組み込みツールは結果を除いた呼び出し情報だけ保存します。"""
        response = await observe_chatbot_api_call(
            "reply_generation",
            self.state.model,
            super().get_response(*args, **kwargs),
            custom_profile=(self.state.profile or {}).get("name"),
        )
        self.completed_responses += 1
        for item in response.output:
            if item.type.endswith("_call") and item.type != "function_call":
                record = item.model_dump(exclude_none=True)
                self.state.calls.append(
                    {"type": item.type, "id": item.id, "action": record.get("action"), "status": record.get("status")}
                )
        return response


def build_agent_tools(tools: ConversationTools, state: ReplyConversation, message_id: int) -> list[Tool]:
    """既存の取得処理をSDKのツールへ接続します。

    Args:
        tools: Discordの権限確認と情報取得を行う処理。
        state: 結果本文を含めず呼び出し履歴を追記する会話状態。
        message_id: 呼び出しの起点となった投稿ID。

    Returns:
        Web検索・コード実行と、既存のスキーマを使う関数ツールの一覧。

    """
    return [
        WebSearchTool(),
        CodeInterpreterTool(tool_config={"type": "code_interpreter", "container": {"type": "auto"}}),
        *[_function_tool(definition, tools, state, message_id) for definition in FUNCTION_TOOLS],
    ]


def _function_tool(
    definition: dict[str, Any], tools: ConversationTools, state: ReplyConversation, message_id: int
) -> FunctionTool:
    """取得結果をSDK形式に変換し、成功・失敗を記録する関数ツールを作ります。"""
    name = definition["name"]

    async def invoke(_context: ToolContext, arguments: str) -> str | list[ToolOutputImage | ToolOutputFileContent]:
        """取得失敗はモデルへ返し、画像・PDFはテキスト化せずSDKへ渡します。"""
        try:
            output = await tools.execute(name, arguments)
            status = "completed"
        except (discord.HTTPException, ValueError, KeyError, TypeError) as exc:
            output = json.dumps({"error": str(exc)}, ensure_ascii=False)
            status = "failed"
        state.calls.append({"name": name, "arguments": arguments, "status": status, "requested_by_message_id": str(message_id)})
        if isinstance(output, str):
            return output
        return [
            ToolOutputImage(image_url=part["image_url"])
            if part["type"] == "input_image"
            else ToolOutputFileContent(file_url=part["file_url"])
            for part in output
        ]

    return FunctionTool(
        name=name,
        description=definition["description"],
        params_json_schema=definition["parameters"],
        on_invoke_tool=invoke,
    )
