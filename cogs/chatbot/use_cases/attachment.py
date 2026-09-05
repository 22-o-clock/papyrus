import asyncio
from logging import getLogger
from typing import Any, cast

from openai import AsyncOpenAI

from cogs.chatbot.constants import ATTACHMENT_CONTEXT_MAX_CHARACTERS
from cogs.chatbot.models import AttachmentAnalysis
from cogs.chatbot.observability import observe_chatbot_api_call
from cogs.chatbot.repositories.short_term_message import ChatbotShortTermMessageRepository
from cogs.chatbot.responses_api import AttachmentInMemory, ShortTermMemory

logger = getLogger(__name__)


class AttachmentUseCases:
    """Chatbotの添付ファイル解析と短期文脈への反映を担当する。"""

    def __init__(
        self,
        message_repository: ChatbotShortTermMessageRepository,
        short_term_memories: dict[int, ShortTermMemory],
        background_tasks: set[asyncio.Task[None]],
    ) -> None:
        self._message_repository = message_repository
        self._short_term_memories = short_term_memories
        self._background_tasks = background_tasks

    def get_kind(self, content_type: str | None) -> str | None:
        """短期文脈の解析対象にする添付種別を返す。"""
        if content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return "image"
        if content_type == "application/pdf":
            return "pdf"
        return None

    def schedule(self, message_id: int, attachment_id: int, filename: str, url: str, kind: str) -> None:
        """添付内容の要約を、投稿処理を待たせずに生成する。"""
        self.update_context(
            message_id,
            AttachmentInMemory(
                attachment_id=attachment_id,
                filename=filename,
                kind=kind,
                analysis_status="pending",
            ),
        )
        task = asyncio.create_task(self._analyze(message_id, attachment_id, filename, url, kind))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _analyze(self, message_id: int, attachment_id: int, filename: str, url: str, kind: str) -> None:
        """画像またはPDFを解析し、短い説明と重要テキストを保存する。"""
        content_type = "input_image" if kind == "image" else "input_file"
        content_key = "image_url" if kind == "image" else "file_url"
        analysis_input = cast(
            "Any",
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "添付内容をそれぞれ100文字以内で短く要約してください。"
                                "画像やPDF内で会話の理解に重要な文字情報があれば重要テキストに抜粋し、"
                                "なければ空文字にしてください。"
                            ),
                        },
                        {"type": content_type, content_key: url},
                    ],
                }
            ],
        )
        try:
            response = await observe_chatbot_api_call(
                "attachment_analysis",
                "gpt-5.4-mini",
                AsyncOpenAI().responses.parse(
                    model="gpt-5.4-mini",
                    input=analysis_input,
                    text_format=AttachmentAnalysis,
                ),
            )
        except Exception:
            logger.exception("Failed to analyze chatbot attachment (attachment_id=%s)", attachment_id)
            await self._save_failure(message_id, attachment_id, filename, kind)
            return
        if response.output_parsed is None:
            logger.warning("Failed to parse chatbot attachment analysis (attachment_id=%s)", attachment_id)
            await self._save_failure(message_id, attachment_id, filename, kind)
            return

        summary = self.truncate_context(response.output_parsed.summary)
        important_text = self.truncate_context(response.output_parsed.important_text)
        await self._message_repository.save_attachment_analysis(
            attachment_id,
            summary=summary,
            important_text=important_text,
            status="completed",
        )
        self.update_context(
            message_id,
            AttachmentInMemory(
                attachment_id=attachment_id,
                filename=filename,
                kind=kind,
                analysis_status="completed",
                summary=summary,
                important_text=important_text,
            ),
        )

    async def _save_failure(self, message_id: int, attachment_id: int, filename: str, kind: str) -> None:
        """解析失敗をDBと稼働中の短期文脈へ反映する。"""
        await self._message_repository.save_attachment_analysis(
            attachment_id,
            summary=None,
            important_text=None,
            status="failed",
        )
        self.update_context(
            message_id,
            AttachmentInMemory(
                attachment_id=attachment_id,
                filename=filename,
                kind=kind,
                analysis_status="failed",
            ),
        )

    def truncate_context(self, text: str | None) -> str | None:
        """添付の解析結果を会話文脈用の上限以内に収める。"""
        return None if text is None else text[:ATTACHMENT_CONTEXT_MAX_CHARACTERS]

    def update_context(self, message_id: int, attachment: AttachmentInMemory) -> None:
        """解析結果を稼働中の全短期記憶へ反映する。"""
        for memory in self._short_term_memories.values():
            memory.set_attachment_analysis(message_id, attachment)
