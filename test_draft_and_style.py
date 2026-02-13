import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import dotenv
from openai import AsyncOpenAI

from cogs.chatbot.responses_api import (
    DraftGenerator,
    MessageInMemory,
    ResponseStyler,
    ShortTermMemory,
)

# ログ出力の設定
log_dir = Path("./log")
log_dir.mkdir(exist_ok=True)

log_file = log_dir / f"test_draft_and_style_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


async def load_sample_conversations():
    """sample_conversations.json からデータを読み込み、会話グループのリストを返す"""
    sample_file = Path("./cogs/chatbot/prompt/tests/data/sample_conversations.json")

    with sample_file.open("r", encoding="utf-8") as f:
        conversations = json.load(f)

    return conversations


def create_short_term_memory_from_conversation(conversation_group):
    """会話グループからShortTermMemoryを生成する"""
    short_term_memory = ShortTermMemory()

    for msg_data in conversation_group:
        message_in_memory = MessageInMemory(
            message_id=hash(msg_data["content"]) & 0x7FFFFFFF,  # content のハッシュをID として使用
            author_name=msg_data["author_name"],
            content=msg_data["content"],
            reply_to=msg_data.get("reply_to", "All"),
            timestamp=datetime.now(),
        )
        short_term_memory.memory.append(message_in_memory)

    return short_term_memory


async def main():
    dotenv.load_dotenv()

    # OpenAI API クライアントの初期化
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set in environment variables")

    client = AsyncOpenAI(api_key=api_key)

    # sample_conversations.json から全ての会話グループを読み込む
    logger.info("Loading conversations...")
    conversations = await load_sample_conversations()
    logger.info(f"Loaded {len(conversations)} conversation groups\n")

    draft_generator = DraftGenerator(client)
    response_styler = ResponseStyler(client)
    bot_name = "Papyrus"

    # 各会話グループを処理
    for idx, conversation_group in enumerate(conversations, 1):
        logger.info("\n" + "=" * 60)
        logger.info(f"Processing conversation group {idx}/{len(conversations)}")
        logger.info("=" * 60)

        # 会話グループからShortTermMemoryを生成
        short_term_memory = create_short_term_memory_from_conversation(conversation_group)
        logger.info(f"\nMessages ({len(short_term_memory.memory)}):")
        logger.info(short_term_memory.to_json())

        # DraftGenerator で初期ドラフトを生成
        logger.info("\n" + "-" * 60)
        logger.info("Generating draft...")
        logger.info("-" * 60)

        try:
            draft = await draft_generator.draft(bot_name, short_term_memory)
            logger.info(f"\nGenerated Draft:\n{draft.content}")
            logger.info(f"\nDraft JSON:\n{draft.to_json(bot_name)}")
        except Exception as e:
            logger.error(f"Error generating draft: {e}")
            continue

        # ResponseStyler でスタイリング
        logger.info("\n" + "-" * 60)
        logger.info("Styling response...")
        logger.info("-" * 60)

        try:
            styled_response = await response_styler.style(bot_name, short_term_memory, draft)
            logger.info(f"\nStyled Response:\n{styled_response.content}")
            logger.info(f"\nStyled Response JSON:\n{styled_response.to_json(bot_name)}")
        except Exception as e:
            logger.error(f"Error styling response: {e}")
            continue


if __name__ == "__main__":
    logger.info("Starting test_draft_and_style.py")
    asyncio.run(main())
    logger.info("Completed test_draft_and_style.py")
