import json
from typing import Any

from pydantic import BaseModel, Field


class ConversationAttachment(BaseModel):
    """添付の取得先と、再構成時に残す解析済みテキストを保持します。"""

    attachment_id: int
    filename: str
    url: str
    kind: str
    summary: str = ""
    important_text: str = ""


class ConversationMessage(BaseModel):
    """会話に取り込んだ時点の投稿と返信先、添付情報を保持します。"""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    created_at: str
    content: str
    parent_id: int | None = None
    parent_channel_id: int | None = None
    is_assistant: bool = False
    attachments: list[ConversationAttachment] = Field(default_factory=list)

    def as_input(self, *, include_media: bool) -> dict[str, Any]:
        """投稿をResponses APIの入力へ変換します。

        Args:
            include_media: ユーザー投稿の画像・PDF本体も入力に含めるか。

        Returns:
            ユーザー投稿は本文、チャンネルIDを除いたメタデータ、添付本体の順の入力。
            Bot投稿は回答本文だけを持つ入力。

        """
        metadata = self.model_dump(exclude={"content", "channel_id", "attachments"})
        metadata["attachments"] = [a.model_dump(exclude={"url"}) for a in self.attachments]

        parts: list[dict[str, Any]] = [
            {"type": "input_text", "text": self.content},
            {"type": "input_text", "text": json.dumps({"metadata": metadata}, ensure_ascii=False)},
        ]
        if include_media and not self.is_assistant:
            for attachment in self.attachments:
                if attachment.kind == "image":
                    parts.append({"type": "input_image", "image_url": attachment.url})
                elif attachment.kind == "pdf":
                    parts.append({"type": "input_file", "file_url": attachment.url})
        if self.is_assistant:
            return {"role": "assistant", "content": self.content}
        return {"role": "user", "content": parts}


class ReplyConversation(BaseModel):
    """返信経路ごとの継続点、要約、結果を含まないツール呼び出し履歴を保持します。"""

    response_id: str | None = None
    model: str = "gpt-5.6-luna"
    messages: list[ConversationMessage] = Field(default_factory=list)
    summary: str = ""
    calls: list[dict[str, Any]] = Field(default_factory=list)
    profile: dict[str, str] | None = None
    missing_history: bool = False
    media_omitted: set[int] = Field(default_factory=set)

    def inputs(self, current_id: int, *, compact: bool) -> list[dict[str, Any]]:
        """保存した会話から継続IDを使わない入力を組み立てます。

        Args:
            current_id: 添付本体を自動で含める今回の呼び出し元ID。
            compact: 過去の添付本体を省略するか。一度省略した本体は再追加しません。

        Returns:
            要約・呼び出し履歴を参考情報として先頭に置いた、時系列順の入力。

        """
        result: list[dict[str, Any]] = []
        if self.summary or self.calls or self.missing_history:
            result.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "reference_only": True,
                            "conversation_summary": self.summary,
                            "past_tool_calls_without_results": self.calls,
                            "missing_history": self.missing_history,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        result.extend(
            m.as_input(include_media=m.message_id == current_id or (not compact and m.message_id not in self.media_omitted))
            for m in self.messages
        )
        return result
