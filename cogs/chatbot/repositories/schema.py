from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import text

from core.db import create_tables_for

from .api_usage import ChatbotApiUsageDaily, ChatbotApiUsageMeasurementState
from .api_usage_report import ApiUsageReportConfiguration, ApiUsageReportDelivery
from .base import CHATBOT_DATABASE_SCHEMA, ChatbotBase
from .custom_profile import ChatbotCustomProfile
from .environment import DatabaseEnvironment
from .long_term_memory import (
    ChatbotLongTermMemory,
    ChatbotLongTermMemoryAdminHistory,
    ChatbotLongTermMemoryChange,
    ChatbotLongTermMemoryEvidence,
)
from .member_alias import ChatbotMemberAlias, ChatbotMemberAliasEvidence, ChatbotMemberAliasHistory
from .memory_extraction_queue import ChatbotMemoryExtractionQueue
from .shadow_candidate import ChatbotShadowCandidate, ChatbotShadowEvaluation
from .short_term_message import ChatbotStoredAttachment, ChatbotStoredMessage

REGISTERED_MODELS: tuple[type[ChatbotBase], ...] = (
    ChatbotApiUsageDaily,
    ChatbotApiUsageMeasurementState,
    DatabaseEnvironment,
    ChatbotCustomProfile,
    ChatbotLongTermMemory,
    ChatbotLongTermMemoryAdminHistory,
    ChatbotLongTermMemoryChange,
    ChatbotLongTermMemoryEvidence,
    ChatbotMemberAlias,
    ChatbotMemberAliasEvidence,
    ChatbotMemberAliasHistory,
    ChatbotMemoryExtractionQueue,
    ChatbotShadowCandidate,
    ChatbotShadowEvaluation,
    ChatbotStoredAttachment,
    ChatbotStoredMessage,
    ApiUsageReportConfiguration,
    ApiUsageReportDelivery,
)

__all__ = ["CHATBOT_DATABASE_SCHEMA", "create_chatbot_tables"]


async def create_chatbot_tables(engine: AsyncEngine) -> None:
    """chatbotスキーマのテーブルと意味検索用拡張を作成します。"""
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS chatbot.chatbot_long_term_memories "
                "ADD COLUMN IF NOT EXISTS superseded_by_memory_id UUID, "
                "ADD COLUMN IF NOT EXISTS conflict_group_id UUID, "
                "ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ"
            )
        )
    await create_tables_for(engine, ChatbotBase.metadata, schema=CHATBOT_DATABASE_SCHEMA)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE chatbot.chatbot_long_term_memories AS memory SET observed_at = source.observed_at "
                "FROM (SELECT evidence.memory_id, MIN(message.created_at) AS observed_at "
                "FROM chatbot.chatbot_long_term_memory_evidences AS evidence "
                "JOIN chatbot.chatbot_stored_messages AS message ON message.message_id = evidence.message_id "
                "GROUP BY evidence.memory_id) AS source "
                "WHERE memory.id = source.memory_id AND memory.observed_at IS NULL"
            )
        )
        await connection.execute(
            text("UPDATE chatbot.chatbot_long_term_memories SET observed_at = created_at WHERE observed_at IS NULL")
        )
