import json
import unittest

from cogs.chatbot.channel_roles import CHANNEL_ROLES_KEY, ChannelRole, ChannelRoleManager


class FakeDatabaseEnvManager:
    """ChannelRoleManagerの永続化境界を確認するためのインメモリ実装。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get_env(self, key: str) -> str | None:
        return self.values.get(key)

    async def set_env(self, key: str, value: str) -> None:
        self.values[key] = value


class ChannelRoleManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_assistant_when_channel_is_not_configured(self) -> None:
        manager = ChannelRoleManager(FakeDatabaseEnvManager())

        role = await manager.get_role(channel_id=100)

        if role is not ChannelRole.ASSISTANT:
            self.fail("未設定チャンネルがassistantになっていません")

    async def test_persists_role_by_channel_id(self) -> None:
        env_manager = FakeDatabaseEnvManager()
        manager = ChannelRoleManager(env_manager)

        await manager.set_role(channel_id=100, role=ChannelRole.CHAT)

        if await manager.get_role(channel_id=100) is not ChannelRole.CHAT:
            self.fail("保存したchat役割を取得できません")
        if await manager.get_role(channel_id=200) is not ChannelRole.ASSISTANT:
            self.fail("別の未設定チャンネルに役割が反映されています")

        serialized_roles = env_manager.values[CHANNEL_ROLES_KEY]
        if json.loads(serialized_roles) != {"100": "chat"}:
            self.fail("チャンネルIDをキーとして役割が保存されていません")

    async def test_returns_assistant_for_invalid_stored_data(self) -> None:
        env_manager = FakeDatabaseEnvManager({CHANNEL_ROLES_KEY: '{"100": "unknown"}'})
        manager = ChannelRoleManager(env_manager)

        role = await manager.get_role(channel_id=100)

        if role is not ChannelRole.ASSISTANT:
            self.fail("不正な役割で安全な既定値にフォールバックしていません")
