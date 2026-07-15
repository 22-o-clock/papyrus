import re

ASSISTANT_DEBOUNCE_SECONDS = 2.0
CHAT_DEBOUNCE_MIN_SECONDS = 5.0
CHAT_DEBOUNCE_MAX_SECONDS = 15.0
CHAT_TEXT_COOLDOWN_SECONDS = 15 * 60
CHAT_REACTION_COOLDOWN_SECONDS = 2 * 60
DEFAULT_CONVERSATION_RESET_MINUTES = 12 * 60
MINIMUM_CONVERSATION_RESET_MINUTES = 1
CONVERSATION_RESET_MINUTES_KEY = "CHATBOT_CONVERSATION_RESET_MINUTES"
DEFAULT_UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES = 30
DEFAULT_UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES = 60
UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES_KEY = "CHATBOT_UNANSWERED_QUESTION_MINIMUM_WAIT_MINUTES"
UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES_KEY = "CHATBOT_UNANSWERED_QUESTION_MAXIMUM_WAIT_MINUTES"
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

QUESTION_ENDING_PATTERN = re.compile(r"(?:\?|ですか|ますか|でしょうか|かな|の\?|何\?|どう\?|誰\?|どこ\?|いつ\?)$")
SHADOW_EVALUATION_FIELDS = (
    "action_appropriate",
    "context_understood",
    "identity_correct",
    "length_natural",
    "non_intrusive",
    "worth_posting",
)
SHADOW_EVALUATION_VALUES = {"◯", "\u00d7", "△"}
SHADOW_REVIEW_HEADERS = {
    "trigger_message": "反応元メッセージ",
    "target_message": "反応対象メッセージ",
    "conversation_context": "会話抜粋",
    "action": "選択した行動",
    "content": "生成文",
    "reaction_emoji": "リアクション",
    "reason": "判断理由",
    "action_appropriate": "行動選択の適切さ",
    "context_understood": "文脈の理解",
    "identity_correct": "人物の区別",
    "length_natural": "長さの自然さ",
    "non_intrusive": "邪魔でない",
    "worth_posting": "総合評価",
    "comment": "コメント",
    "created_at": "作成日時",
    "candidate_id": "候補ID",
    "channel_id": "チャンネルID",
    "trigger_message_id": "反応元メッセージID",
    "reply_to_message_id": "反応対象メッセージID",
    "context_message_ids": "文脈メッセージID一覧",
}
SHADOW_ACTION_LABELS = {
    "silence": "沈黙",
    "reaction": "リアクション",
    "reply": "返信",
    "message": "通常投稿",
}
SHADOW_REASON_LABELS = {
    "natural_contribution": "自然な会話",
    "helpful_unanswered_question": "未回答質問への回答",
    "avoid_interrupting_humans": "人間の会話を優先",
    "no_helpful_contribution": "有益な回答ができない",
    "identity_uncertain": "発言者を区別できない",
    "cooldown": "クールダウン中",
}
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
