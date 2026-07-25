ASSISTANT_DEBOUNCE_SECONDS = 2.0
CHAT_DEBOUNCE_MIN_SECONDS = 5.0
CHAT_DEBOUNCE_MAX_SECONDS = 15.0
CHAT_TEXT_COOLDOWN_SECONDS = 15 * 60
CHAT_REACTION_COOLDOWN_SECONDS = 2 * 60
DEFAULT_CONVERSATION_RESET_MINUTES = 12 * 60
MINIMUM_CONVERSATION_RESET_MINUTES = 1
CONVERSATION_RESET_MINUTES_KEY = "CHATBOT_CONVERSATION_RESET_MINUTES"
ATTACHMENT_CONTEXT_MAX_CHARACTERS = 100
MEMORY_EXTRACTION_BATCH_SIZE = 5
MEMORY_EXTRACTION_WAIT_SECONDS = 10 * 60
MEMORY_SEARCH_CONTEXT_MESSAGE_COUNT = 10
MEMORY_SEARCH_MAXIMUM_COSINE_DISTANCE = 0.70
HISTORY_SYNC_INITIAL_LOOKBACK_HOURS = 12
HISTORY_SYNC_MAXIMUM_LOOKBACK_DAYS = 30
MEMORY_RECONCILIATION_VERSION_KEY = "CHATBOT_MEMORY_RECONCILIATION_VERSION"
MEMORY_RECONCILIATION_VERSION = "2"
DISCORD_RESPONSE_CHUNK_LENGTH = 1900

MEMBER_ALIAS_SHEET_NAME = "別名管理"
MEMBER_ALIAS_MEMBER_SHEET_NAME = "メンバー一覧"
MEMBER_ALIAS_ACTION_LABELS = {
    "keep": "変更なし",
    "change_target": "対象者を変更",
    "invalidate": "無効化",
}
MEMBER_ALIAS_STATUS_LABELS = {
    "active": "有効",
    "ambiguous": "曖昧",
    "invalidated": "無効",
}
MEMBER_ALIAS_HEADERS = (
    "処理",
    "別名",
    "変更後の対象者",
    "現在の対象者",
    "状態",
    "根拠",
    "投稿リンク",
    "更新日時",
    "別名ID",
    "対象者ID",
    "正規化別名",
)
MEMBER_ALIAS_EVIDENCE_COLUMN = 6
LONG_TERM_MEMORY_SHEET_NAME = "長期記憶管理"
LONG_TERM_MEMORY_MANIFEST_SHEET_NAME = "出力記憶ID"
LONG_TERM_MEMORY_ACTION_LABELS = {"keep": "変更なし", "update": "更新", "invalidate": "無効化", "activate": "有効化"}
LONG_TERM_MEMORY_KIND_LABELS = {"profile": "プロフィール", "ongoing": "継続中", "temporary": "一時的", "shared": "共有"}
LONG_TERM_MEMORY_SOURCE_LABELS = {"self_statement": "本人発言", "third_party": "第三者発言", "inference": "推測"}
LONG_TERM_MEMORY_STATUS_LABELS = {
    "active": "有効",
    "invalidated": "無効",
    "superseded": "置換済み",
    "conflicted": "競合",
    "expired": "期限切れ",
}
LONG_TERM_MEMORY_HEADERS = (
    "処理",
    "内容",
    "対象種別",
    "変更後の対象",
    "現在の対象",
    "種類",
    "情報源",
    "機微情報",
    "有効期限",
    "状態",
    "根拠",
    "投稿リンク",
    "元投稿日時",
    "作成日時",
    "置換先ID",
    "競合グループID",
    "記憶ID",
)
LONG_TERM_MEMORY_EVIDENCE_COLUMN = 11
