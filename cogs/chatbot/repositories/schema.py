from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import text

from core.db import create_tables_for

from .api_usage import ChatbotApiUsageDaily, ChatbotApiUsageMeasurementState
from .api_usage_report import ApiUsageReportConfiguration, ApiUsageReportDelivery
from .base import CHATBOT_DATABASE_SCHEMA, ChatbotBase
from .custom_profile import ChatbotCustomProfile
from .environment import DatabaseEnvironment
from .member_alias import ChatbotMemberAlias, ChatbotMemberAliasEvidence, ChatbotMemberAliasHistory
from .memory_document import ChatbotMemoryDocument, ChatbotMemoryProcessingCursor, ChatbotMemoryUpdateJob
from .short_term_message import ChatbotStoredAttachment, ChatbotStoredMessage, ChatbotStoredReactionSnapshot

REGISTERED_MODELS: tuple[type[ChatbotBase], ...] = (
    ChatbotApiUsageDaily,
    ChatbotApiUsageMeasurementState,
    DatabaseEnvironment,
    ChatbotCustomProfile,
    ChatbotMemberAlias,
    ChatbotMemberAliasEvidence,
    ChatbotMemberAliasHistory,
    ChatbotMemoryDocument,
    ChatbotMemoryProcessingCursor,
    ChatbotMemoryUpdateJob,
    ChatbotStoredAttachment,
    ChatbotStoredMessage,
    ChatbotStoredReactionSnapshot,
    ApiUsageReportConfiguration,
    ApiUsageReportDelivery,
)

__all__ = ["CHATBOT_DATABASE_SCHEMA", "create_chatbot_tables"]


async def create_chatbot_tables(engine: AsyncEngine) -> None:
    """chatbotスキーマのテーブルを作成し、既存テーブルを後方互換で拡張します。"""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS chatbot.api_usage_daily "
                "ADD COLUMN IF NOT EXISTS cache_write_input_tokens BIGINT NOT NULL DEFAULT 0, "
                "ADD COLUMN IF NOT EXISTS long_context_cache_write_input_tokens BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS chatbot.chatbot_stored_messages "
                "ADD COLUMN IF NOT EXISTS custom_profile_name TEXT, "
                "ADD COLUMN IF NOT EXISTS is_forwarded BOOLEAN NOT NULL DEFAULT FALSE, "
                "ADD COLUMN IF NOT EXISTS is_self BOOLEAN NOT NULL DEFAULT FALSE, "
                "ADD COLUMN IF NOT EXISTS embeds JSONB NOT NULL DEFAULT '[]'::jsonb"
            )
        )
    await create_tables_for(engine, ChatbotBase.metadata, schema=CHATBOT_DATABASE_SCHEMA)
