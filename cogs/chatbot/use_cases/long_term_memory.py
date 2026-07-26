import asyncio
import datetime
import json
from logging import getLogger

import tiktoken
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.constants import (
    CONVERSATION_INACTIVITY_SECONDS,
    MEMORY_DOCUMENT_ATTACHMENT_WAIT_SECONDS,
    MEMORY_DOCUMENT_MESSAGE_TRIGGER,
    MEMORY_DOCUMENT_NEW_CONTEXT_TOKENS,
    MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
    MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS,
    MEMORY_DOCUMENT_TOKEN_TRIGGER,
    MEMORY_DOCUMENT_TOTAL_CONTEXT_TOKENS,
)
from cogs.chatbot.repositories.environment import DatabaseEnvironmentRepository
from cogs.chatbot.repositories.member_alias import normalize_member_alias
from cogs.chatbot.repositories.memory_document import (
    ChatbotMemoryDocumentRepository,
    MemoryAliasInput,
    MemoryDocumentInput,
)
from cogs.chatbot.repositories.short_term_message import (
    ChatbotShortTermMessageRepository,
    ChatbotStoredMessage,
)
from cogs.chatbot.responses_api import MemoryDocumentUpdater, MemoryDocumentUpdateResult
from cogs.chatbot.services.memory_document_format import has_required_memory_document_headings

logger = getLogger(__name__)


class MemoryDocumentValidationError(ValueError):
    """モデルが返した文書集合を安全に一括適用できない場合の例外。"""


class LongTermMemoryUseCases:
    """会話単位で長期記憶Markdown文書を更新するバックグラウンド処理。"""

    def __init__(
        self,
        bot: commands.Bot,
        _environment_repository: DatabaseEnvironmentRepository,
        session_factory: async_sessionmaker[AsyncSession],
        background_tasks: set[asyncio.Task[None]],
    ) -> None:
        self._bot = bot
        self._documents = ChatbotMemoryDocumentRepository(session_factory)
        self._messages = ChatbotShortTermMessageRepository(session_factory)
        self._updater = MemoryDocumentUpdater(AsyncOpenAI())
        self._background_tasks = background_tasks
        self._worker_task: asyncio.Task[None] | None = None
        self._inactivity_tasks: dict[int, asyncio.Task[None]] = {}
        self._encoding = tiktoken.get_encoding("o200k_base")

    async def initialize(self) -> None:
        """中断ジョブと12時間タイマーを復元します。"""
        await self._documents.restore_interrupted()
        for cursor in await self._documents.get_cursors():
            if cursor.last_human_message_at is not None:
                self._schedule_inactivity(cursor.channel_id, cursor.last_human_message_at)
        self._schedule_worker()

    async def enqueue(
        self,
        message_id: int,
        channel_id: int,
        *,
        is_human: bool,
        created_at: datetime.datetime,
    ) -> None:
        """人間またはPapyrusの新規投稿を更新判定へ渡します。"""
        if is_human:
            queued_inactivity = False
            cursor = await self._documents.get_cursor(channel_id)
            if (
                cursor is not None
                and cursor.last_human_message_at is not None
                and (created_at - cursor.last_human_message_at).total_seconds() >= CONVERSATION_INACTIVITY_SECONDS
            ):
                preceding_messages = await self._messages.get_range(
                    channel_id,
                    after_message_id=cursor.last_processed_message_id,
                    through_message_id=message_id - 1,
                )
                eligible = [message for message in preceding_messages if self._is_update_source(message)]
                if eligible:
                    await self._documents.enqueue(
                        channel_id,
                        preceding_messages[-1].message_id,
                        "inactivity",
                        wait_for_attachments=False,
                    )
                    self._schedule_worker()
                    queued_inactivity = True
            await self._documents.note_human_message(channel_id, created_at)
            self._schedule_inactivity(channel_id, created_at)
            if queued_inactivity:
                return
        await self._enqueue_if_threshold_reached(channel_id, message_id)

    async def delete(self, _message_id: int) -> None:
        """未処理投稿の削除は保存済み会話からの削除だけで反映されます。"""

    def _schedule_inactivity(self, channel_id: int, last_human_message_at: datetime.datetime) -> None:
        previous = self._inactivity_tasks.pop(channel_id, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(self._enqueue_after_inactivity(channel_id, last_human_message_at))
        self._inactivity_tasks[channel_id] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _enqueue_after_inactivity(
        self,
        channel_id: int,
        last_human_message_at: datetime.datetime,
    ) -> None:
        deadline = last_human_message_at + datetime.timedelta(seconds=CONVERSATION_INACTIVITY_SECONDS)
        delay = max(0.0, (deadline - datetime.datetime.now(datetime.UTC)).total_seconds())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        cursor = await self._documents.get_cursor(channel_id)
        if cursor is None or cursor.last_human_message_at != last_human_message_at:
            return
        messages = await self._messages.get_range(
            channel_id,
            after_message_id=cursor.last_processed_message_id,
        )
        eligible = [message for message in messages if self._is_update_source(message)]
        if not eligible:
            return
        await self._documents.enqueue(
            channel_id,
            messages[-1].message_id,
            "inactivity",
            wait_for_attachments=False,
        )
        self._schedule_worker()

    async def _enqueue_if_threshold_reached(self, channel_id: int, latest_message_id: int) -> None:
        cursor = await self._documents.get_cursor(channel_id)
        messages = await self._messages.get_range(
            channel_id,
            after_message_id=cursor.last_processed_message_id if cursor is not None else None,
            through_message_id=latest_message_id,
        )
        source_count = sum(self._is_update_source(message) for message in messages)
        token_count = sum(self._message_token_count(message) for message in messages)
        if source_count < MEMORY_DOCUMENT_MESSAGE_TRIGGER and token_count < MEMORY_DOCUMENT_TOKEN_TRIGGER:
            return

        selected: list[ChatbotStoredMessage] = []
        selected_tokens = 0
        for message in messages:
            message_tokens = self._message_token_count(message)
            if selected and selected_tokens + message_tokens > MEMORY_DOCUMENT_NEW_CONTEXT_TOKENS:
                break
            selected.append(message)
            selected_tokens += message_tokens
        if not selected:
            return
        await self._documents.enqueue(
            channel_id,
            selected[-1].message_id,
            "message_count" if source_count >= MEMORY_DOCUMENT_MESSAGE_TRIGGER else "token_count",
            wait_for_attachments=True,
        )
        self._schedule_worker()

    def _schedule_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        task = asyncio.create_task(self._run_worker())
        self._worker_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_worker(self) -> None:
        """サーバー内のジョブをFIFOで処理し、失敗はそのチャンネルだけ戻します。"""
        while True:
            job = await self._documents.claim_next()
            if job is None:
                return
            try:
                await self._process_job(
                    job.channel_id,
                    job.end_message_id,
                    wait_for_attachments=job.wait_for_attachments,
                )
            except Exception:
                logger.exception(
                    "Failed to update chatbot memory documents (channel_id=%s, end_message_id=%s)",
                    job.channel_id,
                    job.end_message_id,
                )
                await self._documents.mark_failed(job.channel_id)

    async def _process_job(
        self,
        channel_id: int,
        end_message_id: int,
        *,
        wait_for_attachments: bool,
    ) -> None:
        cursor = await self._documents.get_cursor(channel_id)
        all_messages = await self._messages.get_range(channel_id, after_message_id=None, through_message_id=end_message_id)
        new_messages = [
            message
            for message in all_messages
            if cursor is None
            or cursor.last_processed_message_id is None
            or message.message_id > cursor.last_processed_message_id
        ]
        if not new_messages:
            await self._documents.complete(channel_id, end_message_id, [], [])
            return
        if wait_for_attachments:
            await self._wait_for_attachments([message.message_id for message in new_messages])

        new_payload = await self._serialize_messages(new_messages)
        new_tokens = self._token_count(new_payload)
        reference_budget = max(0, MEMORY_DOCUMENT_TOTAL_CONTEXT_TOKENS - new_tokens)
        earlier = all_messages[: len(all_messages) - len(new_messages)]
        reference_messages: list[ChatbotStoredMessage] = []
        used_reference_tokens = 0
        for message in reversed(earlier):
            message_tokens = self._message_token_count(message)
            if used_reference_tokens + message_tokens > reference_budget:
                break
            reference_messages.append(message)
            used_reference_tokens += message_tokens
        reference_messages.reverse()

        existing_documents = await self._documents.get_all()
        members = list(self._bot.get_all_members())
        payload: dict[str, object] = {
            "existing_documents": [
                {
                    "document_key": document.document_key,
                    "document_type": document.document_type,
                    "target_user_id": document.target_user_id,
                    "content": document.content,
                }
                for document in existing_documents
            ],
            "reference_messages": await self._serialize_messages(reference_messages),
            "new_messages": new_payload,
            "members": [
                {
                    "user_id": member.id,
                    "display_name": member.display_name,
                    "username": member.name,
                }
                for member in members
            ],
        }
        result = await self._updater.update(payload)
        if self._needs_document_retry(result):
            payload["proposed_result"] = result.model_dump()
            payload["character_limits"] = {
                "person": MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
                "bot": MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
                "shared": MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS,
            }
            result = await self._updater.update(payload, shorten=True)
        documents = self._validate_documents(result, {member.id for member in members})
        aliases = self._validate_aliases(result, new_messages, {member.id for member in members})
        await self._documents.complete(channel_id, end_message_id, documents, aliases)
        latest_message_id = await self._messages.get_latest_message_id(channel_id)
        if latest_message_id is not None and latest_message_id > end_message_id:
            await self._enqueue_if_threshold_reached(channel_id, latest_message_id)

    async def _wait_for_attachments(self, message_ids: list[int]) -> None:
        deadline = asyncio.get_running_loop().time() + MEMORY_DOCUMENT_ATTACHMENT_WAIT_SECONDS
        while await self._messages.has_pending_attachments(message_ids):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1.0, remaining))

    async def _serialize_messages(self, messages: list[ChatbotStoredMessage]) -> list[dict[str, object]]:
        attachments = await self._messages.get_attachments([message.message_id for message in messages])
        by_message_id: dict[int, list[dict[str, object]]] = {}
        for attachment in attachments:
            by_message_id.setdefault(attachment.message_id, []).append(
                {
                    "filename": attachment.filename,
                    "kind": attachment.kind,
                    "analysis_status": attachment.analysis_status,
                    "summary": attachment.summary,
                    "important_text": attachment.important_text,
                }
            )
        return [
            {
                "message_id": message.message_id,
                "author_id": message.author_id,
                "author_name": message.author_name,
                "content": message.content,
                "reply_to_message_id": message.reply_to_message_id,
                "mentioned_user_ids": message.mentioned_user_ids,
                "created_at": message.created_at.isoformat(),
                "is_bot": message.is_bot,
                "is_papyrus": message.is_self,
                "is_forwarded": message.is_forwarded,
                "custom_profile_name": message.custom_profile_name,
                "embeds": message.embeds,
                "attachments": by_message_id.get(message.message_id, []),
            }
            for message in messages
        ]

    def _validate_documents(
        self,
        result: MemoryDocumentUpdateResult,
        member_ids: set[int],
    ) -> list[MemoryDocumentInput]:
        documents: list[MemoryDocumentInput] = []
        seen_keys: set[str] = set()
        for update_result in result.updates:
            expected_key = update_result.document_type
            maximum = MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS
            if update_result.document_type == "person":
                if update_result.target_user_id not in member_ids:
                    raise MemoryDocumentValidationError
                expected_key = f"person:{update_result.target_user_id}"
                maximum = MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS
            elif update_result.target_user_id is not None:
                raise MemoryDocumentValidationError
            elif update_result.document_type == "bot":
                maximum = MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS
            if update_result.document_key != expected_key or expected_key in seen_keys:
                raise MemoryDocumentValidationError
            if len(update_result.content) > maximum:
                raise MemoryDocumentValidationError
            if not has_required_memory_document_headings(update_result.document_type, update_result.content):
                raise MemoryDocumentValidationError
            seen_keys.add(expected_key)
            documents.append(
                MemoryDocumentInput(
                    document_key=expected_key,
                    document_type=update_result.document_type,
                    target_user_id=update_result.target_user_id,
                    content=update_result.content.strip(),
                )
            )
        return documents

    def _validate_aliases(
        self,
        result: MemoryDocumentUpdateResult,
        messages: list[ChatbotStoredMessage],
        member_ids: set[int],
    ) -> list[MemoryAliasInput]:
        messages_by_id = {message.message_id: message for message in messages}
        member_names = {
            member.id: {normalize_member_alias(member.display_name), normalize_member_alias(member.name)}
            for member in self._bot.get_all_members()
        }
        aliases: list[MemoryAliasInput] = []
        for candidate in result.aliases:
            normalized = normalize_member_alias(candidate.alias)
            if (
                candidate.target_user_id not in member_ids
                or not normalized
                or normalized in member_names[candidate.target_user_id]
            ):
                continue
            evidence = [
                messages_by_id[message_id] for message_id in candidate.evidence_message_ids if message_id in messages_by_id
            ]
            if not evidence:
                continue
            aliases.append(
                MemoryAliasInput(
                    alias=candidate.alias,
                    target_user_id=candidate.target_user_id,
                    evidence_message_ids=[message.message_id for message in evidence],
                    evidence_author_ids=[message.author_id for message in evidence],
                    evidence_excerpts=[message.content for message in evidence],
                )
            )
        return aliases

    def _needs_document_retry(self, result: MemoryDocumentUpdateResult) -> bool:
        return any(
            len(document.content)
            > (
                MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS
                if document.document_type == "shared"
                else MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS
            )
            or not has_required_memory_document_headings(document.document_type, document.content)
            for document in result.updates
        )

    def _is_update_source(self, message: ChatbotStoredMessage) -> bool:
        return not message.is_forwarded and (not message.is_bot or message.is_self)

    def _message_token_count(self, message: ChatbotStoredMessage) -> int:
        return self._token_count(
            {
                "author": message.author_name,
                "content": message.content,
                "embeds": message.embeds,
            }
        )

    def _token_count(self, value: object) -> int:
        return len(self._encoding.encode(json.dumps(value, ensure_ascii=False, default=str)))
