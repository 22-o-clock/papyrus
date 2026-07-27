from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tiktoken

from cogs.chatbot.constants import PENDING_OTHER_CHANNEL_CONTEXT_TOKENS

if TYPE_CHECKING:
    from discord.ext import commands

    from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
    from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocument, ChatbotMemoryDocumentRepository
    from cogs.chatbot.repositories.short_term_message import ChatbotShortTermMessageRepository, ChatbotStoredMessage
    from cogs.chatbot.responses_api import ResponsePipeline


@dataclass(frozen=True, slots=True)
class ResponseMemoryContext:
    """応答生成へ渡す長期記憶と、必要時だけ取得する未反映情報。"""

    long_term_memory: str
    pending_index: str = ""
    pending_context: str = ""


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
        target_user_ids = await self._get_target_user_ids(channel_id, resolved_member_aliases)
        documents = await self._memory_repository.get_for_users(target_user_ids)
        return self._serialize_documents(documents)

    async def build_response_memory(
        self,
        channel_id: int,
        resolved_member_aliases: dict[str, int] | None = None,
    ) -> ResponseMemoryContext:
        """長期記憶と他チャンネルの未反映情報を一貫したスナップショットで返します。"""
        target_user_ids = await self._get_target_user_ids(channel_id, resolved_member_aliases)
        documents, pending_messages = await self._memory_repository.get_response_snapshot(
            target_user_ids,
            exclude_channel_id=channel_id,
        )
        pending_index, pending_context = self._serialize_pending_messages(pending_messages)
        return ResponseMemoryContext(
            long_term_memory=self._serialize_documents(documents),
            pending_index=pending_index,
            pending_context=pending_context,
        )

    async def _get_target_user_ids(
        self,
        channel_id: int,
        resolved_member_aliases: dict[str, int] | None,
    ) -> set[int]:
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
        return target_user_ids

    @staticmethod
    def _serialize_documents(documents: list[ChatbotMemoryDocument]) -> str:
        return "\n\n".join(
            f"## {document.document_key}\n{document.content}" for document in documents if document.content.strip()
        )

    def _serialize_pending_messages(self, messages: list[ChatbotStoredMessage]) -> tuple[str, str]:
        eligible = [message for message in messages if not message.is_long_term_memory_excluded]
        source_channel_ids = {
            message.channel_id for message in eligible if not message.is_forwarded and (not message.is_bot or message.is_self)
        }
        eligible = [message for message in eligible if message.channel_id in source_channel_ids]
        if not eligible:
            return "", ""

        grouped: dict[int, list[ChatbotStoredMessage]] = {}
        for message in eligible:
            grouped.setdefault(message.channel_id, []).append(message)
        index = {
            "channels": [
                {
                    "channel_id": channel_id,
                    "channel_name": self._channel_name(channel_id),
                    "message_count": len(channel_messages),
                    "participants": sorted({message.author_name for message in channel_messages}),
                    "latest_at": channel_messages[-1].created_at.isoformat(timespec="minutes"),
                }
                for channel_id, channel_messages in sorted(grouped.items())
            ]
        }

        encoding = tiktoken.get_encoding("o200k_base")
        selected: list[ChatbotStoredMessage] = []
        for message in reversed(eligible):
            candidate = [message, *selected]
            serialized = self._serialize_pending_context(candidate)
            if selected and len(encoding.encode(serialized)) > PENDING_OTHER_CHANNEL_CONTEXT_TOKENS:
                break
            selected = candidate
        return (
            json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            self._serialize_pending_context(selected),
        )

    def _serialize_pending_context(self, messages: list[ChatbotStoredMessage]) -> str:
        grouped: dict[int, list[ChatbotStoredMessage]] = {}
        for message in messages:
            grouped.setdefault(message.channel_id, []).append(message)
        return json.dumps(
            {
                "channels": [
                    {
                        "channel_id": channel_id,
                        "channel_name": self._channel_name(channel_id),
                        "messages": [
                            {
                                "message_id": message.message_id,
                                "author_id": message.author_id,
                                "author_name": message.author_name,
                                "created_at": message.created_at.isoformat(timespec="minutes"),
                                "content": message.content,
                                "reply_to_message_id": message.reply_to_message_id,
                                "mentioned_user_ids": message.mentioned_user_ids,
                            }
                            for message in channel_messages
                        ],
                    }
                    for channel_id, channel_messages in sorted(grouped.items())
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _channel_name(self, channel_id: int) -> str:
        channel = self._bot.get_channel(channel_id)
        return getattr(channel, "name", str(channel_id))
