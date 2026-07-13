import json

from cogs.chatbot.responses_api import MessageInMemory


def get_latest_memory_search_query(message: MessageInMemory) -> str:
    """最新投稿は識別用メタデータを除き、本文の意味を優先して記憶検索へ使います。"""
    content = message.content.strip()
    return content or json.dumps(message.to_dict(), ensure_ascii=False)
