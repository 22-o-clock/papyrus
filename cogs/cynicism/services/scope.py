"""実行環境に応じた集計対象チャンネルの決定。"""

from cogs.cynicism.models import ChannelScope
from core.runtime_environment import RuntimeEnvironment


def channel_scope(runtime_environment: RuntimeEnvironment) -> ChannelScope:
    """Chatbotの処理対象と同じ範囲を、分子と分母の両方へ適用できる形で返す。"""
    test_channel_ids = frozenset(runtime_environment.chatbot_test_channel_ids)
    if runtime_environment.is_debug:
        # デバッグBotはテストチャンネルの中だけで完結させ、本番の集計へ影響させない。
        return ChannelScope(included_channel_ids=test_channel_ids, excluded_channel_ids=frozenset())
    return ChannelScope(included_channel_ids=None, excluded_channel_ids=test_channel_ids)
