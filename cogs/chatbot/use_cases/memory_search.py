from __future__ import annotations

import json
from logging import getLogger
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from cogs.chatbot.constants import MEMORY_SEARCH_CONTEXT_MESSAGE_COUNT, MEMORY_SEARCH_MAXIMUM_COSINE_DISTANCE
from cogs.chatbot.observability import observe_chatbot_api_call
from cogs.chatbot.repositories.member_alias import find_user_ids_by_member_alias
from cogs.chatbot.use_cases.memory_query import get_latest_memory_search_query

if TYPE_CHECKING:
    import uuid

    from discord.ext import commands

    from cogs.chatbot.repositories.long_term_memory import ChatbotLongTermMemory, ChatbotLongTermMemoryRepository
    from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
    from cogs.chatbot.responses_api import ResponsePipeline

logger = getLogger(__name__)


class MemorySearchUseCases:
    """短期会話に関連する長期記憶の検索と整形を担当する。"""

    def __init__(
        self,
        bot: commands.Bot,
        response_pipelines: dict[int, ResponsePipeline],
        memory_repository: ChatbotLongTermMemoryRepository,
        alias_repository: ChatbotMemberAliasRepository,
    ) -> None:
        self._bot = bot
        self._response_pipelines = response_pipelines
        self._memory_repository = memory_repository
        self._alias_repository = alias_repository

    async def build_response_context(self, channel_id: int) -> str:
        """直近会話に意味的に近い有効記憶を応答用テキストへ整形する。"""
        try:
            short_term_memory = self._response_pipelines[channel_id].short_term_memory
            search_messages = short_term_memory.memory[-MEMORY_SEARCH_CONTEXT_MESSAGE_COUNT:]
            if not search_messages:
                return ""
            search_context = json.dumps(
                [memory_message.to_dict(include_reactions=False) for memory_message in search_messages],
                ensure_ascii=False,
            )
            search_queries = [get_latest_memory_search_query(search_messages[-1])]
            if len(search_messages) > 1:
                search_queries.append(search_context)
            embedding_response = await observe_chatbot_api_call(
                "memory_search_embedding",
                "text-embedding-3-large",
                AsyncOpenAI().embeddings.create(
                    model="text-embedding-3-large",
                    input=search_queries,
                ),
                item_count=len(search_queries),
            )
            target_user_ids = {
                user_id
                for memory_message in search_messages
                for user_id in (memory_message.author_id, *memory_message.mentioned_user_ids)
            }
            normalized_context = search_context.casefold()
            for member in self._bot.get_all_members():
                known_names = {member.display_name.casefold(), member.name.casefold()}
                if any(name and name in normalized_context for name in known_names):
                    target_user_ids.add(member.id)
            active_aliases = await self._alias_repository.get_active_aliases()
            target_user_ids.update(find_user_ids_by_member_alias(search_context, active_aliases))
            memories_by_id: dict[uuid.UUID, ChatbotLongTermMemory] = {}
            for embedding_data in embedding_response.data:
                memories = await self._memory_repository.search(
                    embedding_data.embedding,
                    target_user_ids,
                    MEMORY_SEARCH_MAXIMUM_COSINE_DISTANCE,
                    20,
                )
                for memory in memories:
                    memories_by_id.setdefault(memory.id, memory)
            selected = list(memories_by_id.values())[:20]
        except Exception:
            logger.exception("Failed to search chatbot long-term memories (channel_id=%s)", channel_id)
            return ""
        logger.info(
            "Selected chatbot long-term memories (channel_id=%s, target_user_ids=%s, memory_ids=%s)",
            channel_id,
            sorted(target_user_ids),
            [str(memory.id) for memory in selected],
        )
        return "\n".join(
            f"- [target={memory.target_user_id or memory.external_entity_name or 'shared'}; "
            f"kind={memory.kind}; source={memory.source_type}] {memory.content}"
            for memory in selected
        )
