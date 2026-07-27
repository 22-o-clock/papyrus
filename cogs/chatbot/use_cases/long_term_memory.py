import asyncio
import datetime
import json
from logging import getLogger
from typing import Any

import tiktoken
from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.constants import (
    MEMORY_DOCUMENT_ATTACHMENT_WAIT_SECONDS,
    MEMORY_DOCUMENT_BATCH_JOB_ID,
    MEMORY_DOCUMENT_BOT_MAX_CHARACTERS,
    MEMORY_DOCUMENT_BOT_SHORTEN_TRIGGER_CHARACTERS,
    MEMORY_DOCUMENT_BOT_TARGET_CHARACTERS,
    MEMORY_DOCUMENT_DAILY_FLUSH_HOUR,
    MEMORY_DOCUMENT_MESSAGE_TRIGGER,
    MEMORY_DOCUMENT_NEW_CONTEXT_TOKENS,
    MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
    MEMORY_DOCUMENT_PERSON_SHORTEN_TRIGGER_CHARACTERS,
    MEMORY_DOCUMENT_PERSON_TARGET_CHARACTERS,
    MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS,
    MEMORY_DOCUMENT_SHARED_SHORTEN_TRIGGER_CHARACTERS,
    MEMORY_DOCUMENT_SHARED_TARGET_CHARACTERS,
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
from cogs.chatbot.services.memory_document_format import (
    MEMORY_DOCUMENT_HEADINGS,
    has_required_memory_document_headings,
)
from cogs.chatbot.services.prompt_context import omit_empty_values

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
        self._daily_flush_task: asyncio.Task[None] | None = None
        self._encoding = tiktoken.get_encoding("o200k_base")

    async def initialize(self) -> None:
        """中断ジョブを復元し、サーバー共通workerと午前4時flushを開始します。"""
        await self._documents.restore_interrupted()
        await self._enqueue_if_threshold_reached()
        self._schedule_worker()
        self._schedule_daily_flush()

    async def enqueue(
        self,
        message_id: int,
        channel_id: int,
        *,
        is_human: bool,
        created_at: datetime.datetime,
    ) -> None:
        """新規投稿後にサーバー全体の未反映量を判定します。"""
        del channel_id, is_human, created_at
        await self._enqueue_if_threshold_reached(message_id)

    async def delete(self, _message_id: int) -> None:
        """未処理投稿の削除は保存済み会話からの削除だけで反映されます。"""

    def _schedule_daily_flush(self) -> None:
        if self._daily_flush_task is not None and not self._daily_flush_task.done():
            return
        task = asyncio.create_task(self._run_daily_flush())
        self._daily_flush_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_daily_flush(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_daily_flush(datetime.datetime.now(datetime.UTC)))
            messages = await self._documents.get_pending_messages()
            if any(self._is_update_source(message) for message in messages):
                await self._documents.enqueue(
                    MEMORY_DOCUMENT_BATCH_JOB_ID,
                    messages[-1].message_id,
                    "daily_flush",
                    wait_for_attachments=True,
                )
                self._schedule_worker()

    @staticmethod
    def _seconds_until_daily_flush(now: datetime.datetime) -> float:
        local_timezone = datetime.timezone(datetime.timedelta(hours=9))
        local_now = now.astimezone(local_timezone)
        deadline = datetime.datetime.combine(
            local_now.date(),
            datetime.time(hour=MEMORY_DOCUMENT_DAILY_FLUSH_HOUR),
            tzinfo=local_timezone,
        )
        if deadline <= local_now:
            deadline += datetime.timedelta(days=1)
        return (deadline - local_now).total_seconds()

    async def _enqueue_if_threshold_reached(self, latest_message_id: int | None = None) -> None:
        messages = await self._documents.get_pending_messages()
        memory_evidence = [message for message in messages if self._is_memory_evidence(message)]
        source_count = sum(self._is_update_source(message) for message in memory_evidence)
        token_count = sum(self._message_token_count(message) for message in memory_evidence)
        if source_count == 0 or (
            source_count < MEMORY_DOCUMENT_MESSAGE_TRIGGER and token_count < MEMORY_DOCUMENT_TOKEN_TRIGGER
        ):
            return

        await self._documents.enqueue(
            MEMORY_DOCUMENT_BATCH_JOB_ID,
            latest_message_id if latest_message_id is not None else messages[-1].message_id,
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
        """サーバー共通の長期記憶更新ジョブを直列処理します。"""
        while True:
            job = await self._documents.claim_next()
            if job is None:
                return
            try:
                await self._process_job(
                    wait_for_attachments=job.wait_for_attachments,
                    flush_all=job.trigger in {"daily_flush", "daily_flush_remaining"},
                )
            except Exception:
                logger.exception(
                    "Failed to update chatbot memory documents (end_message_id=%s)",
                    job.end_message_id,
                )
                await self._documents.mark_failed(MEMORY_DOCUMENT_BATCH_JOB_ID)

    async def _process_job(
        self,
        *,
        wait_for_attachments: bool,
        flush_all: bool,
    ) -> None:
        pending_messages = await self._documents.get_pending_messages()
        new_messages = self._select_pending_batch(pending_messages)
        if not new_messages:
            await self._documents.complete_batch({}, [], [])
            return
        if wait_for_attachments:
            await self._wait_for_attachments([message.message_id for message in new_messages])

        reference_messages = await self._get_reference_messages(new_messages)
        existing_documents = await self._documents.get_all()
        members = list(self._bot.get_all_members())
        payload = await self._build_update_payload(new_messages, reference_messages, existing_documents, members)
        result = await self._updater.update(payload)
        result = await self._shorten_documents_if_needed(result)
        documents = self._validate_documents(result, {member.id for member in members})
        aliases = self._validate_aliases(
            result,
            [message for message in new_messages if self._is_memory_evidence(message)],
            {member.id for member in members},
        )
        selected_channel_ids = {message.channel_id for message in new_messages}
        end_message_ids = {
            selected_channel_id: max(
                message.message_id for message in new_messages if message.channel_id == selected_channel_id
            )
            for selected_channel_id in selected_channel_ids
        }
        await self._documents.complete_batch(end_message_ids, documents, aliases)
        await self._continue_batch_if_needed(flush_all=flush_all, end_message_ids=end_message_ids)

    def _select_pending_batch(self, pending_messages: list[ChatbotStoredMessage]) -> list[ChatbotStoredMessage]:
        memory_evidence = [message for message in pending_messages if self._is_memory_evidence(message)]
        first_source_index = next(
            (index for index, message in enumerate(memory_evidence) if self._is_update_source(message)),
            None,
        )
        if first_source_index is None:
            return []
        selected: list[ChatbotStoredMessage] = []
        selected_tokens = 0
        for message in memory_evidence[first_source_index:]:
            message_tokens = self._message_token_count(message)
            if selected and selected_tokens + message_tokens > MEMORY_DOCUMENT_NEW_CONTEXT_TOKENS:
                break
            selected.append(message)
            selected_tokens += message_tokens
        return selected

    async def _get_reference_messages(
        self,
        new_messages: list[ChatbotStoredMessage],
    ) -> list[ChatbotStoredMessage]:
        new_tokens = sum(self._message_token_count(message) for message in new_messages)
        reference_budget = max(0, MEMORY_DOCUMENT_TOTAL_CONTEXT_TOKENS - new_tokens)
        cursors = {cursor.channel_id: cursor for cursor in await self._documents.get_cursors()}
        earlier: list[ChatbotStoredMessage] = []
        for channel_id in {message.channel_id for message in new_messages}:
            cursor = cursors.get(channel_id)
            if cursor is not None and cursor.last_processed_message_id is not None:
                earlier.extend(
                    await self._messages.get_range(
                        channel_id,
                        after_message_id=None,
                        through_message_id=cursor.last_processed_message_id,
                    )
                )
        earlier.sort(key=lambda message: (message.created_at, message.message_id))
        selected: list[ChatbotStoredMessage] = []
        selected_tokens = 0
        for message in reversed(earlier):
            message_tokens = self._message_token_count(message)
            if selected_tokens + message_tokens > reference_budget:
                break
            selected.append(message)
            selected_tokens += message_tokens
        selected.reverse()
        return selected

    async def _build_update_payload(
        self,
        new_messages: list[ChatbotStoredMessage],
        reference_messages: list[ChatbotStoredMessage],
        existing_documents: list[Any],
        members: list[Any],
    ) -> dict[str, object]:
        context_messages = [*reference_messages, *new_messages]
        channel_conversations: list[dict[str, object]] = []
        for channel_id in sorted({message.channel_id for message in new_messages}):
            channel = self._bot.get_channel(channel_id)
            channel_conversations.append(
                {
                    "channel_id": channel_id,
                    "channel_name": getattr(channel, "name", str(channel_id)),
                    "reference_messages": await self._serialize_messages(
                        [message for message in reference_messages if message.channel_id == channel_id]
                    ),
                    "new_messages": await self._serialize_messages(
                        [message for message in new_messages if message.channel_id == channel_id]
                    ),
                }
            )
        return {
            "existing_documents": [
                {
                    "document_key": document.document_key,
                    "document_type": document.document_type,
                    "target_user_id": document.target_user_id,
                    "content": document.content,
                }
                for document in existing_documents
            ],
            "message_authors": {str(message.author_id): message.author_name for message in context_messages},
            "channel_conversations": channel_conversations,
            "members": [
                {
                    "user_id": member.id,
                    "display_name": member.display_name,
                    "username": member.name,
                }
                for member in members
            ],
        }

    async def _continue_batch_if_needed(
        self,
        *,
        flush_all: bool,
        end_message_ids: dict[int, int],
    ) -> None:
        if flush_all:
            remaining = await self._documents.get_pending_messages()
            if any(self._is_update_source(message) for message in remaining):
                await self._documents.enqueue(
                    MEMORY_DOCUMENT_BATCH_JOB_ID,
                    remaining[-1].message_id,
                    "daily_flush_remaining",
                    wait_for_attachments=True,
                )
                return
        await self._enqueue_if_threshold_reached(max(end_message_ids.values()))

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
        return [self._serialize_message(message, attachments=by_message_id.get(message.message_id, [])) for message in messages]

    @staticmethod
    def _serialize_message(
        message: ChatbotStoredMessage,
        *,
        attachments: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return omit_empty_values(
            {
                "i": message.message_id,
                "a": message.author_id,
                "t": message.created_at.isoformat(timespec="minutes"),
                "c": message.content,
                "r": message.reply_to_message_id,
                "u": message.mentioned_user_ids,
                "b": message.is_bot,
                "p": message.is_self,
                "f": message.is_forwarded,
                "g": message.is_long_term_memory_excluded,
                "o": message.custom_profile_name,
                "e": message.embeds,
                "x": attachments or [],
            }
        )

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
                maximum = MEMORY_DOCUMENT_BOT_MAX_CHARACTERS
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

    async def _shorten_documents_if_needed(
        self,
        result: MemoryDocumentUpdateResult,
    ) -> MemoryDocumentUpdateResult:
        documents = [
            document
            for document in result.updates
            if len(document.content) > self._document_character_limits(document.document_type)[1]
            or not has_required_memory_document_headings(document.document_type, document.content)
        ]
        if not documents:
            return result

        expected_keys = {document.document_key for document in documents}
        if len(expected_keys) != len(documents):
            raise MemoryDocumentValidationError
        shortened = await self._updater.shorten(
            {
                "documents": [
                    {
                        "content": document.content,
                        "target_characters": self._document_character_limits(document.document_type)[0],
                        "required_headings": MEMORY_DOCUMENT_HEADINGS[document.document_type],
                    }
                    for document in documents
                ]
            }
        )
        if len(shortened.contents) != len(documents):
            raise MemoryDocumentValidationError
        replacements = {
            original.document_key: original.model_copy(update={"content": content})
            for original, content in zip(documents, shortened.contents, strict=True)
        }

        return MemoryDocumentUpdateResult(
            updates=[replacements.get(document.document_key, document) for document in result.updates],
            aliases=result.aliases,
        )

    @staticmethod
    def _document_character_limits(document_type: str) -> tuple[int, int, int]:
        if document_type == "person":
            return (
                MEMORY_DOCUMENT_PERSON_TARGET_CHARACTERS,
                MEMORY_DOCUMENT_PERSON_SHORTEN_TRIGGER_CHARACTERS,
                MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
            )
        if document_type == "bot":
            return (
                MEMORY_DOCUMENT_BOT_TARGET_CHARACTERS,
                MEMORY_DOCUMENT_BOT_SHORTEN_TRIGGER_CHARACTERS,
                MEMORY_DOCUMENT_BOT_MAX_CHARACTERS,
            )
        if document_type == "shared":
            return (
                MEMORY_DOCUMENT_SHARED_TARGET_CHARACTERS,
                MEMORY_DOCUMENT_SHARED_SHORTEN_TRIGGER_CHARACTERS,
                MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS,
            )
        raise MemoryDocumentValidationError

    def _is_update_source(self, message: ChatbotStoredMessage) -> bool:
        return self._is_memory_evidence(message) and not message.is_forwarded and (not message.is_bot or message.is_self)

    @staticmethod
    def _is_memory_evidence(message: ChatbotStoredMessage) -> bool:
        return not message.is_long_term_memory_excluded

    def _message_token_count(self, message: ChatbotStoredMessage) -> int:
        return self._token_count(self._serialize_message(message))

    def _token_count(self, value: object) -> int:
        return len(
            self._encoding.encode(
                json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")),
            )
        )
