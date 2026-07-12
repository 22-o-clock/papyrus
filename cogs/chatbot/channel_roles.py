import asyncio
import json
from enum import StrEnum
from logging import getLogger
from typing import Protocol

logger = getLogger(__name__)

CHANNEL_ROLES_KEY = "CHATBOT_CHANNEL_ROLES"


class ChannelRole(StrEnum):
    """Chatbotがチャンネル内で担う役割。"""

    ASSISTANT = "assistant"
    CHAT = "chat"


class DatabaseEnvStore(Protocol):
    """チャンネル役割の保存に必要な設定ストアの操作。"""

    async def get_env(self, key: str) -> str | None: ...

    async def set_env(self, key: str, value: str) -> None: ...


class ChannelRoleManager:
    """チャンネルごとのChatbot役割を永続化します。"""

    def __init__(self, env_manager: DatabaseEnvStore) -> None:
        self._env_manager = env_manager
        self._write_lock = asyncio.Lock()

    async def get_role(self, channel_id: int, parent_channel_id: int | None = None) -> ChannelRole:
        """チャンネルの役割を取得し、未設定のスレッドでは親チャンネルから継承します。"""
        roles = await self._load_roles()
        for candidate_channel_id in (channel_id, parent_channel_id):
            if candidate_channel_id is None:
                continue
            configured_role = roles.get(str(candidate_channel_id))
            if configured_role is None:
                continue

            try:
                return ChannelRole(configured_role)
            except ValueError:
                logger.warning(
                    "Unknown chatbot channel role (channel_id=%s, role=%r)",
                    candidate_channel_id,
                    configured_role,
                )

        return ChannelRole.ASSISTANT

    async def get_configured_role(self, channel_id: int) -> ChannelRole | None:
        """チャンネル自体に明示的に保存された役割を取得します。"""
        roles = await self._load_roles()
        configured_role = roles.get(str(channel_id))
        if configured_role is None:
            return None

        try:
            return ChannelRole(configured_role)
        except ValueError:
            logger.warning("Unknown chatbot channel role (channel_id=%s, role=%r)", channel_id, configured_role)
            return None

    async def set_role(self, channel_id: int, role: ChannelRole) -> None:
        """チャンネルの役割を保存します。"""
        async with self._write_lock:
            roles = await self._load_roles()
            roles[str(channel_id)] = role.value
            await self._env_manager.set_env(CHANNEL_ROLES_KEY, json.dumps(roles, ensure_ascii=False, sort_keys=True))

    async def clear_role(self, channel_id: int) -> None:
        """チャンネル固有の役割を削除し、既定値または親チャンネルの継承へ戻します。"""
        async with self._write_lock:
            roles = await self._load_roles()
            roles.pop(str(channel_id), None)
            await self._env_manager.set_env(CHANNEL_ROLES_KEY, json.dumps(roles, ensure_ascii=False, sort_keys=True))

    async def _load_roles(self) -> dict[str, str]:
        """保存済み設定を読み込み、壊れている場合は空の設定として扱います。"""
        serialized_roles = await self._env_manager.get_env(CHANNEL_ROLES_KEY)
        if serialized_roles is None:
            return {}

        try:
            loaded_roles = json.loads(serialized_roles)
        except json.JSONDecodeError:
            logger.warning("Failed to decode chatbot channel roles")
            return {}

        if not isinstance(loaded_roles, dict) or not all(
            isinstance(channel_id, str) and isinstance(role, str) for channel_id, role in loaded_roles.items()
        ):
            logger.warning("Invalid chatbot channel roles format")
            return {}

        return loaded_roles
