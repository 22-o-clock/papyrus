import json
import unittest

from cogs.chatbot.channel_roles import CHANNEL_ROLES_KEY, ChannelRole, ChannelRoleManager


class FakeDatabaseEnvironmentRepository:
    """ChannelRoleManagerの永続化境界を確認するためのインメモリ実装。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get_env(self, key: str) -> str | None:
        return self.values.get(key)

    async def set_env(self, key: str, value: str) -> None:
        self.values[key] = value


class ChannelRoleManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_assistant_when_channel_is_not_configured(self) -> None:
        manager = ChannelRoleManager(FakeDatabaseEnvironmentRepository())

        role = await manager.get_role(channel_id=100)

        if role is not ChannelRole.ASSISTANT:
            self.fail("未設定チャンネルがassistantになっていません")

    async def test_persists_role_by_channel_id(self) -> None:
        environment_repository = FakeDatabaseEnvironmentRepository()
        manager = ChannelRoleManager(environment_repository)

        await manager.set_role(channel_id=100, role=ChannelRole.CHAT)

        if await manager.get_role(channel_id=100) is not ChannelRole.CHAT:
            self.fail("保存したchat役割を取得できません")
        if await manager.get_role(channel_id=200) is not ChannelRole.ASSISTANT:
            self.fail("別の未設定チャンネルに役割が反映されています")

        serialized_roles = environment_repository.values[CHANNEL_ROLES_KEY]
        if json.loads(serialized_roles) != {"100": "chat"}:
            self.fail("チャンネルIDをキーとして役割が保存されていません")

    async def test_returns_assistant_for_invalid_stored_data(self) -> None:
        environment_repository = FakeDatabaseEnvironmentRepository({CHANNEL_ROLES_KEY: '{"100": "unknown"}'})
        manager = ChannelRoleManager(environment_repository)

        role = await manager.get_role(channel_id=100)

        if role is not ChannelRole.ASSISTANT:
            self.fail("不正な役割で安全な既定値にフォールバックしていません")

    async def test_thread_inherits_parent_channel_role(self) -> None:
        environment_repository = FakeDatabaseEnvironmentRepository({CHANNEL_ROLES_KEY: '{"100": "chat"}'})
        manager = ChannelRoleManager(environment_repository)

        role = await manager.get_role(channel_id=200, parent_channel_id=100)

        if role is not ChannelRole.CHAT:
            self.fail("未設定スレッドが親チャンネルの役割を継承していません")

    async def test_thread_override_takes_priority_over_parent(self) -> None:
        environment_repository = FakeDatabaseEnvironmentRepository(
            {CHANNEL_ROLES_KEY: '{"100": "chat", "200": "assistant"}'},
        )
        manager = ChannelRoleManager(environment_repository)

        role = await manager.get_role(channel_id=200, parent_channel_id=100)

        if role is not ChannelRole.ASSISTANT:
            self.fail("スレッド固有の役割が親チャンネルより優先されていません")

    async def test_clear_thread_override_restores_inheritance(self) -> None:
        environment_repository = FakeDatabaseEnvironmentRepository(
            {CHANNEL_ROLES_KEY: '{"100": "chat", "200": "assistant"}'},
        )
        manager = ChannelRoleManager(environment_repository)

        await manager.clear_role(channel_id=200)

        if await manager.get_configured_role(channel_id=200) is not None:
            self.fail("スレッド固有の役割を解除できません")
        if await manager.get_role(channel_id=200, parent_channel_id=100) is not ChannelRole.CHAT:
            self.fail("固有設定の解除後に親チャンネルの役割を継承していません")
