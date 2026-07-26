import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import mapped_column

from .base import ChatbotBase

EMBEDDING_DIMENSIONS = 3072


class ChatbotLegacyLongTermMemory(ChatbotBase):
    """一度限りの文書移行で読み取る旧形式の長期記憶。"""

    __tablename__ = "chatbot_long_term_memories"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_user_id = mapped_column(BigInteger, nullable=True, index=True)
    external_entity_name = mapped_column(Text, nullable=True, index=True)
    target_resolution = mapped_column(Text, nullable=False, index=True)
    kind = mapped_column(Text, nullable=False)
    content = mapped_column(Text, nullable=False)
    source_type = mapped_column(Text, nullable=False)
    is_sensitive = mapped_column(Boolean, nullable=False)
    status = mapped_column(Text, nullable=False, index=True)
    expires_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    invalidated_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    superseded_by_memory_id = mapped_column(UUID(as_uuid=True), nullable=True)
    conflict_group_id = mapped_column(UUID(as_uuid=True), nullable=True)
    observed_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    embedding = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
