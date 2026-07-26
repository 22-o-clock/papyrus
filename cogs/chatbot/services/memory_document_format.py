MEMORY_DOCUMENT_HEADINGS = {
    "person": (
        "# 人物の記憶",
        "## 基本情報",
        "## 嗜好・関心",
        "## 関係・継続事項",
    ),
    "bot": (
        "# Papyrusの自己記憶",
        "## 嗜好・立場",
        "## 人物への印象・関係",
        "## 継続的な約束",
    ),
    "shared": (
        "# 共有記憶",
        "## 共有されている前提",
        "## 継続中の話題・決定",
    ),
}


def has_required_memory_document_headings(document_type: str, content: str) -> bool:
    """文書種別ごとの固定見出しが順番どおり一度ずつ存在するか返します。"""
    required = MEMORY_DOCUMENT_HEADINGS.get(document_type)
    if required is None:
        return False
    headings = [line.strip() for line in content.splitlines() if line.startswith("#")]
    return headings == list(required)
