import json

from .channel_roles import DatabaseEnvironmentRepositoryProtocol

SHADOW_MODE_CHANNELS_KEY = "CHATBOT_SHADOW_MODE_CHANNELS"


class ShadowModeManager:
    """チャンネルごとのシャドーモード設定を永続化します。"""

    def __init__(self, environment_repository: DatabaseEnvironmentRepositoryProtocol) -> None:
        self._environment_repository = environment_repository

    async def is_enabled(self, channel_id: int) -> bool:
        """指定チャンネルでシャドーモードが有効か取得します。"""
        return str(channel_id) in await self._load_channel_ids()

    async def set_enabled(self, channel_id: int, *, enabled: bool) -> None:
        """指定チャンネルのシャドーモードを保存します。"""
        await self._environment_repository.update_json_string_set_member(
            SHADOW_MODE_CHANNELS_KEY,
            str(channel_id),
            enabled=enabled,
        )

    async def _load_channel_ids(self) -> set[str]:
        """有効化済みチャンネルIDを読み込みます。"""
        serialized_channel_ids = await self._environment_repository.get_env(SHADOW_MODE_CHANNELS_KEY)
        if serialized_channel_ids is None:
            return set()
        try:
            loaded_channel_ids = json.loads(serialized_channel_ids)
        except json.JSONDecodeError:
            return set()
        if not isinstance(loaded_channel_ids, list) or not all(
            isinstance(channel_id, str) for channel_id in loaded_channel_ids
        ):
            return set()
        return {channel_id for channel_id in loaded_channel_ids if isinstance(channel_id, str)}
