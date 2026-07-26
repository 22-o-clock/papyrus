"""旧長期記憶を確認用Markdownへ出力し、確認後の文書を一度だけ適用する。"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import dotenv
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cogs.chatbot.prompt import load_prompt
from cogs.chatbot.repositories.environment import DatabaseEnvironment
from cogs.chatbot.repositories.legacy_memory import ChatbotLegacyLongTermMemory
from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocument
from cogs.chatbot.services.memory_migration import parse_memory_migration_markdown
from core.db import create_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MIGRATION_KEY = "CHATBOT_MEMORY_DOCUMENT_MIGRATION_VERSION"
MIGRATION_VERSION = "1"
MEMORY_MIGRATION_INSTRUCTIONS = load_prompt("long_term_memory_migration.md")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("output", type=Path)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("input", type=Path)
    return parser


async def _export(output: Path) -> None:
    session_factory = _session_factory()
    now = datetime.datetime.now(datetime.UTC)
    async with session_factory() as session:
        result = await session.execute(
            select(ChatbotLegacyLongTermMemory)
            .where(
                ChatbotLegacyLongTermMemory.status == "active",
                (ChatbotLegacyLongTermMemory.expires_at.is_(None)) | (ChatbotLegacyLongTermMemory.expires_at > now),
                (ChatbotLegacyLongTermMemory.target_user_id.is_not(None)) | (ChatbotLegacyLongTermMemory.kind == "shared"),
            )
            .order_by(ChatbotLegacyLongTermMemory.observed_at, ChatbotLegacyLongTermMemory.created_at)
        )
        memories = [
            {
                "target_user_id": memory.target_user_id,
                "kind": memory.kind,
                "content": memory.content,
                "observed_at": (memory.observed_at or memory.created_at).date().isoformat(),
            }
            for memory in result.scalars()
            if memory.target_user_id is not None or memory.kind == "shared"
        ]
    response = await AsyncOpenAI().responses.create(
        model="gpt-5.6-luna",
        reasoning={"effort": "medium"},
        instructions=MEMORY_MIGRATION_INSTRUCTIONS,
        input=json.dumps({"memories": memories}, ensure_ascii=False),
    )
    await asyncio.to_thread(output.write_text, response.output_text.strip() + "\n", encoding="utf-8")


async def _apply(input_path: Path) -> None:
    content = await asyncio.to_thread(input_path.read_text, encoding="utf-8")
    documents = parse_memory_migration_markdown(content)
    session_factory = _session_factory()
    now = datetime.datetime.now(datetime.UTC)
    async with session_factory.begin() as session:
        current_version = await session.scalar(
            select(DatabaseEnvironment.value).where(DatabaseEnvironment.key == MIGRATION_KEY)
        )
        if current_version is not None:
            message = f"移行は適用済みです (version={current_version})"
            raise RuntimeError(message)
        for document_key, content in documents.items():
            document_type = "person" if document_key.startswith("person:") else document_key
            target_user_id = int(document_key.removeprefix("person:")) if document_type == "person" else None
            statement = insert(ChatbotMemoryDocument).values(
                document_key=document_key,
                document_type=document_type,
                target_user_id=target_user_id,
                content=content,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatbotMemoryDocument.document_key],
                    set_={
                        "document_type": document_type,
                        "target_user_id": target_user_id,
                        "content": content,
                        "updated_at": now,
                    },
                )
            )
        await session.execute(insert(DatabaseEnvironment).values(key=MIGRATION_KEY, value=MIGRATION_VERSION))


def _session_factory() -> async_sessionmaker[AsyncSession]:
    dotenv.load_dotenv()
    _engine, session_factory = create_session_factory(
        os.environ["CHATBOT_SUPABASE_CONNECTION_STRING"],
        search_path="chatbot,extensions,public",
    )
    return session_factory


async def _main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "export":
        await _export(arguments.output)
    else:
        await _apply(arguments.input)


if __name__ == "__main__":
    asyncio.run(_main())
