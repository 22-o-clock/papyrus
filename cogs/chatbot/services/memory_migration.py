from cogs.chatbot.constants import (
    MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS,
    MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS,
)
from cogs.chatbot.services.memory_document_format import has_required_memory_document_headings


def parse_memory_migration_markdown(content: str) -> dict[str, str]:
    """確認済みの移行Markdownを文書キーと本文へ分解し、上限を検証します。"""
    documents: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in content.splitlines():
        if line.startswith("<!-- document:") and line.endswith(" -->"):
            current_key = line.removeprefix("<!-- document:").removesuffix(" -->").strip()
            if current_key in documents:
                message = "同じ文書見出しが複数あります。"
                raise ValueError(message)
            documents[current_key] = []
        elif current_key is not None:
            documents[current_key].append(line)
    parsed = {key: "\n".join(lines).strip() for key, lines in documents.items()}
    _validate_documents(parsed)
    return parsed


def _validate_documents(parsed: dict[str, str]) -> None:
    """移行文書の必須種別、文書キー、文字数を検証します。"""
    if not {"shared", "bot"}.issubset(parsed):
        message = "sharedとbotの文書見出しが必要です。"
        raise ValueError(message)
    for key, body in parsed.items():
        if key not in {"shared", "bot"} and not key.startswith("person:"):
            message = f"不明な文書見出しです: {key}"
            raise ValueError(message)
        maximum = MEMORY_DOCUMENT_SHARED_MAX_CHARACTERS if key == "shared" else MEMORY_DOCUMENT_PERSON_MAX_CHARACTERS
        if len(body) > maximum:
            message = f"{key} が文字数上限を超えています。"
            raise ValueError(message)
        document_type = "person" if key.startswith("person:") else key
        if not has_required_memory_document_headings(document_type, body):
            message = f"{key} のMarkdown見出しが固定形式と一致しません。"
            raise ValueError(message)
        if key.startswith("person:"):
            int(key.removeprefix("person:"))
