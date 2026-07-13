import asyncio
from logging import getLogger

from discord.ext import commands
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cogs.chatbot.constants import (
    MEMORY_EXTRACTION_BATCH_SIZE,
    MEMORY_EXTRACTION_WAIT_SECONDS,
    MEMORY_RECONCILIATION_VERSION,
    MEMORY_RECONCILIATION_VERSION_KEY,
)
from cogs.chatbot.database import (
    ChatbotLongTermMemory,
    ChatbotLongTermMemoryStore,
    ChatbotMemberAliasStore,
    ChatbotMemoryExtractionQueueStore,
    ChatbotShortTermMessageStore,
    LongTermMemoryInput,
    MemberAliasInput,
    MemoryReconciliationInput,
    normalize_member_alias,
)
from cogs.chatbot.database_envs import DatabaseEnvManager
from cogs.chatbot.observability import log_chatbot_api_call
from cogs.chatbot.responses_api import (
    LongTermMemoryCandidate,
    LongTermMemoryCorrectionCandidate,
    LongTermMemoryExtractor,
    LongTermMemoryReconciler,
    MemberAliasCandidate,
    MessageInMemory,
)

logger = getLogger(__name__)


class LongTermMemoryUseCases:
    """長期記憶の抽出キューと既存記憶との整合性を管理します。"""

    def __init__(
        self,
        bot: commands.Bot,
        env_manager: DatabaseEnvManager,
        session_factory: async_sessionmaker[AsyncSession],
        background_tasks: set[asyncio.Task[None]],
    ) -> None:
        self.bot = bot
        self.env_manager = env_manager
        self.short_term_message_store = ChatbotShortTermMessageStore(session_factory)
        self.memory_extraction_queue = ChatbotMemoryExtractionQueueStore(session_factory)
        self.long_term_memory_store = ChatbotLongTermMemoryStore(session_factory)
        self.member_alias_store = ChatbotMemberAliasStore(session_factory)
        self.long_term_memory_extractor = LongTermMemoryExtractor(AsyncOpenAI())
        self.long_term_memory_reconciler = LongTermMemoryReconciler(AsyncOpenAI())
        self._background_tasks = background_tasks
        self._memory_extraction_task: asyncio.Task[None] | None = None
        self._memory_queue_recovered = False
        self._memory_reconciliation_started = False

    async def initialize(self) -> None:
        """中断された抽出を復旧し、既存記憶の一度きりの照合を開始します。"""
        if not self._memory_queue_recovered:
            await self.memory_extraction_queue.recover_interrupted()
            self._memory_queue_recovered = True
        await self._schedule_memory_extraction()
        if self._memory_reconciliation_started:
            return
        self._memory_reconciliation_started = True
        task = asyncio.create_task(self._reconcile_existing_memories_once())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def enqueue(self, message_id: int, channel_id: int) -> None:
        """人間の投稿を抽出待ちへ追加し、処理を予約します。"""
        await self.memory_extraction_queue.enqueue(message_id, channel_id)
        await self._schedule_memory_extraction()

    async def delete(self, message_id: int) -> None:
        """削除された投稿を抽出対象から除外します。"""
        await self.memory_extraction_queue.delete(message_id)

    async def _reconcile_existing_memories_once(self) -> None:
        """導入前から存在する記憶を古い順に一度だけ訂正・競合判定します。"""
        if await self.env_manager.get_env(MEMORY_RECONCILIATION_VERSION_KEY) == MEMORY_RECONCILIATION_VERSION:
            return
        memories = await self.long_term_memory_store.get_all_active_ordered()
        for memory in memories:
            current_active = await self.long_term_memory_store.get_active_for_target(
                memory.target_user_id,
                memory.external_entity_name,
            )
            memory_time = memory.observed_at or memory.created_at
            earlier_memories = [
                candidate for candidate in current_active if (candidate.observed_at or candidate.created_at) < memory_time
            ]
            if not earlier_memories:
                continue
            reconciliation = await self.long_term_memory_reconciler.reconcile(
                self._stored_memory_for_reconciliation(memory),
                [self._stored_memory_for_reconciliation(candidate) for candidate in earlier_memories],
                correction_only=False,
            )
            if reconciliation.action not in {"supersede", "conflict"}:
                continue
            allowed_ids = {candidate.id for candidate in earlier_memories}
            reconciliation_ids = [memory_id for memory_id in reconciliation.existing_memory_ids if memory_id in allowed_ids]
            await self.long_term_memory_store.apply_reconciliation(
                MemoryReconciliationInput(
                    action=reconciliation.action,
                    existing_memory_ids=reconciliation_ids,
                    evidence_message_ids=[],
                ),
                new_memory_id=memory.id,
            )
            logger.info(
                "Reconciled existing chatbot memories (action=%s, new_memory_id=%s, existing_memory_ids=%s)",
                reconciliation.action,
                memory.id,
                reconciliation_ids,
            )
        await self.env_manager.set_env(MEMORY_RECONCILIATION_VERSION_KEY, MEMORY_RECONCILIATION_VERSION)

    async def _schedule_memory_extraction(self) -> None:
        """未処理投稿を一定時間まとめて長期記憶として抽出します。"""
        delay_seconds = (
            0
            if await self.memory_extraction_queue.count_pending() >= MEMORY_EXTRACTION_BATCH_SIZE
            else MEMORY_EXTRACTION_WAIT_SECONDS
        )
        if self._memory_extraction_task is not None and not self._memory_extraction_task.done():
            if delay_seconds != 0:
                return
            self._memory_extraction_task.cancel()
        task = asyncio.create_task(self._extract_long_term_memories_after_wait(delay_seconds))
        self._memory_extraction_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _extract_long_term_memories_after_wait(self, delay_seconds: float) -> None:
        """投稿をまとめて抽出し、失敗時はキューを再試行対象へ戻します。"""
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        while True:
            queue_items = await self.memory_extraction_queue.claim_pending(MEMORY_EXTRACTION_BATCH_SIZE)
            message_ids = [item.message_id for item in queue_items]
            if not message_ids:
                return
            if not await self._extract_long_term_memory_batch(message_ids):
                return
            if len(message_ids) < MEMORY_EXTRACTION_BATCH_SIZE:
                return

    async def _extract_long_term_memory_batch(self, message_ids: list[int]) -> bool:
        """確保済みの投稿群から記憶を抽出し、処理結果をキューへ反映します。"""
        try:
            stored_messages = await self.short_term_message_store.get_by_ids(message_ids)
            messages = [
                MessageInMemory(
                    message_id=message.message_id,
                    author_id=message.author_id,
                    author_name=message.author_name,
                    content=message.content,
                    reply_to_message_id=message.reply_to_message_id,
                    mentioned_user_ids=message.mentioned_user_ids,
                    timestamp=message.created_at,
                )
                for message in stored_messages
            ]
            members = list(self.bot.get_all_members())
            member_ids = {member.id for member in members}
            active_aliases = await self.member_alias_store.get_active_aliases()
            aliases_by_user_id: dict[int, list[str]] = {}
            for alias, target_user_id in active_aliases.items():
                aliases_by_user_id.setdefault(target_user_id, []).append(alias)
            extraction = await self.long_term_memory_extractor.extract(
                messages,
                [
                    {
                        "user_id": member.id,
                        "display_name": member.display_name,
                        "username": member.name,
                        "aliases": aliases_by_user_id.get(member.id, []),
                    }
                    for member in members
                ],
            )
            messages_by_id = {message.message_id: message for message in messages}
            member_names = {
                member.id: {
                    normalize_member_alias(member.display_name),
                    normalize_member_alias(member.name),
                }
                for member in members
            }
            await self._save_extracted_aliases(extraction.aliases, messages_by_id, member_ids, member_names)
            active_aliases = await self.member_alias_store.get_active_aliases()
            for candidate in extraction.candidates:
                await self._save_extracted_memory(candidate, messages_by_id, member_ids, active_aliases)
            for correction in extraction.corrections:
                await self._apply_memory_correction(correction, messages_by_id, member_ids, active_aliases)
        except Exception:
            logger.exception("Failed to extract chatbot long-term memories (message_ids=%s)", message_ids)
            await self.memory_extraction_queue.restore_pending(message_ids)
            return False
        await self.memory_extraction_queue.complete(message_ids)
        return True

    async def _save_extracted_aliases(
        self,
        alias_candidates: list[MemberAliasCandidate],
        messages_by_id: dict[int, MessageInMemory],
        member_ids: set[int],
        member_names: dict[int, set[str]],
    ) -> None:
        """抽出された別名のうち、実在メンバーと根拠を確認できるものだけ保存します。"""
        for alias_candidate in alias_candidates:
            normalized_alias = normalize_member_alias(alias_candidate.alias)
            if alias_candidate.target_user_id not in member_ids or not normalized_alias:
                continue
            if normalized_alias in member_names[alias_candidate.target_user_id]:
                continue
            evidence = [
                messages_by_id[message_id]
                for message_id in alias_candidate.evidence_message_ids
                if message_id in messages_by_id
            ]
            if evidence:
                await self.member_alias_store.save(
                    MemberAliasInput(
                        alias=alias_candidate.alias,
                        target_user_id=alias_candidate.target_user_id,
                        evidence_message_ids=[message.message_id for message in evidence],
                        evidence_author_ids=[message.author_id for message in evidence],
                        evidence_excerpts=[message.content for message in evidence],
                    )
                )

    async def _save_extracted_memory(
        self,
        candidate: LongTermMemoryCandidate,
        messages_by_id: dict[int, MessageInMemory],
        member_ids: set[int],
        active_aliases: dict[str, int],
    ) -> None:
        """新規記憶を保存し、既存記憶との訂正・競合関係を適用します。"""
        evidence = [messages_by_id[message_id] for message_id in candidate.evidence_message_ids if message_id in messages_by_id]
        if not evidence:
            return
        target_user_id = candidate.target_user_id if candidate.target_user_id in member_ids else None
        if target_user_id is None and candidate.external_entity_name:
            target_user_id = active_aliases.get(normalize_member_alias(candidate.external_entity_name))
        external_entity_name = candidate.external_entity_name if target_user_id is None else None
        log_chatbot_api_call("memory_embedding", "text-embedding-3-large")
        embedding_response = await AsyncOpenAI().embeddings.create(model="text-embedding-3-large", input=candidate.content)
        existing_memories = await self.long_term_memory_store.get_active_for_target(target_user_id, external_entity_name)
        reconciliation = await self.long_term_memory_reconciler.reconcile(
            self._memory_candidate_for_reconciliation(candidate),
            [self._stored_memory_for_reconciliation(memory) for memory in existing_memories],
            correction_only=False,
        )
        allowed_ids = {memory.id for memory in existing_memories}
        reconciliation_ids = [memory_id for memory_id in reconciliation.existing_memory_ids if memory_id in allowed_ids]
        if reconciliation.action == "invalidate":
            reconciliation.action = "keep"
            reconciliation_ids = []
        stored_memory_id = await self.long_term_memory_store.save(
            LongTermMemoryInput(
                target_user_id=target_user_id,
                external_entity_name=external_entity_name,
                target_resolution=self._normalize_memory_target_resolution(target_user_id, external_entity_name),
                kind=candidate.kind,
                content=candidate.content,
                source_type=candidate.source_type,
                is_sensitive=candidate.is_sensitive,
                evidence_message_ids=[message.message_id for message in evidence],
                evidence_author_ids=[message.author_id for message in evidence],
                evidence_excerpts=[message.content for message in evidence],
                embedding=embedding_response.data[0].embedding,
                observed_at=min(message.timestamp for message in evidence),
            )
        )
        logger.info(
            "Saved chatbot long-term memory (memory_id=%s, target_user_id=%s, target_resolution=%s, source_type=%s)",
            stored_memory_id,
            target_user_id,
            self._normalize_memory_target_resolution(target_user_id, external_entity_name),
            candidate.source_type,
        )
        await self.long_term_memory_store.apply_reconciliation(
            MemoryReconciliationInput(
                action=reconciliation.action,
                existing_memory_ids=reconciliation_ids,
                evidence_message_ids=[message.message_id for message in evidence],
            ),
            new_memory_id=stored_memory_id,
        )
        if reconciliation.action != "keep" and reconciliation_ids:
            logger.info(
                "Reconciled chatbot memories (action=%s, new_memory_id=%s, existing_memory_ids=%s)",
                reconciliation.action,
                stored_memory_id,
                reconciliation_ids,
            )

    def _memory_candidate_for_reconciliation(self, candidate: LongTermMemoryCandidate) -> dict[str, object]:
        """新規記憶候補を訂正判定モデルの入力へ整形します。"""
        return {"content": candidate.content, "source_type": candidate.source_type}

    def _stored_memory_for_reconciliation(self, memory: ChatbotLongTermMemory) -> dict[str, object]:
        """既存記憶を訂正判定モデルの入力へ整形します。"""
        return {
            "id": str(memory.id),
            "content": memory.content,
            "source_type": memory.source_type,
            "observed_at": (memory.observed_at or memory.created_at).isoformat(),
        }

    async def _apply_memory_correction(
        self,
        correction: LongTermMemoryCorrectionCandidate,
        messages_by_id: dict[int, MessageInMemory],
        member_ids: set[int],
        active_aliases: dict[str, int],
    ) -> None:
        """新しい事実を伴わない明示的否定を、対象が明確な場合だけ適用します。"""
        evidence = [
            messages_by_id[message_id] for message_id in correction.evidence_message_ids if message_id in messages_by_id
        ]
        if not evidence:
            return
        target_user_id = correction.target_user_id if correction.target_user_id in member_ids else None
        if target_user_id is None and correction.external_entity_name:
            target_user_id = active_aliases.get(normalize_member_alias(correction.external_entity_name))
        external_entity_name = correction.external_entity_name if target_user_id is None else None
        existing_memories = await self.long_term_memory_store.get_active_for_target(target_user_id, external_entity_name)
        reconciliation = await self.long_term_memory_reconciler.reconcile(
            {"content": correction.statement, "source_type": correction.source_type},
            [self._stored_memory_for_reconciliation(memory) for memory in existing_memories],
            correction_only=True,
        )
        if reconciliation.action != "invalidate":
            return
        allowed_ids = {memory.id for memory in existing_memories}
        reconciliation_ids = [memory_id for memory_id in reconciliation.existing_memory_ids if memory_id in allowed_ids]
        await self.long_term_memory_store.apply_reconciliation(
            MemoryReconciliationInput(
                action="invalidate",
                existing_memory_ids=reconciliation_ids,
                evidence_message_ids=[message.message_id for message in evidence],
            ),
            new_memory_id=None,
        )
        if reconciliation_ids:
            logger.info(
                "Invalidated chatbot memories from explicit correction (existing_memory_ids=%s, evidence_message_ids=%s)",
                reconciliation_ids,
                [message.message_id for message in evidence],
            )

    def _normalize_memory_target_resolution(
        self,
        target_user_id: int | None,
        external_entity_name: str | None,
    ) -> str:
        """記憶対象の保存形式を、実際に設定された識別子と矛盾しない形へ揃えます。"""
        if target_user_id is not None:
            return "member"
        if external_entity_name:
            return "external"
        return "unresolved"
