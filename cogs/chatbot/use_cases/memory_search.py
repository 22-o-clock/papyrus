from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

    from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
    from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocumentRepository
    from cogs.chatbot.repositories.short_term_message import ChatbotShortTermMessageRepository
    from cogs.chatbot.responses_api import ResponsePipeline


class MemorySearchUseCases:
    """短期会話に登場する人物と共有・Bot文書を応答文脈へ加えます。"""

    def __init__(
        self,
        bot: commands.Bot,
        response_pipelines: dict[int, ResponsePipeline],
        memory_repository: ChatbotMemoryDocumentRepository,
        _alias_repository: ChatbotMemberAliasRepository,
        message_repository: ChatbotShortTermMessageRepository,
    ) -> None:
        self._bot = bot
        self._response_pipelines = response_pipelines
        self._memory_repository = memory_repository
        self._message_repository = message_repository

    async def build_response_context(
        self,
        channel_id: int,
        resolved_member_aliases: dict[str, int] | None = None,
    ) -> str:
        """共有・Bot文書と、短期会話で参照された人物文書を返します。"""
        short_term_memory = self._response_pipelines[channel_id].short_term_memory
        prompt_messages = short_term_memory.get_prompt_messages()
        target_user_ids = {
            user_id for message in prompt_messages for user_id in (message.author_id, *message.mentioned_user_ids)
        }
        reply_message_ids = {
            message.reply_to_message_id for message in prompt_messages if message.reply_to_message_id is not None
        }
        replied_messages = await self._message_repository.get_by_ids(list(reply_message_ids))
        target_user_ids.update(message.author_id for message in replied_messages)
        target_user_ids.update((resolved_member_aliases or {}).values())
        bot_user = self._bot.user
        if bot_user is not None:
            target_user_ids.discard(bot_user.id)
        documents = await self._memory_repository.get_for_users(target_user_ids)
        return "\n\n".join(
            f"## {document.document_key}\n{document.content}" for document in documents if document.content.strip()
        )
