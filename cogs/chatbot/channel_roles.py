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


class DatabaseEnvironmentRepositoryProtocol(Protocol):
    """チャンネル役割の保存に必要な設定ストアの操作。"""

    async def get_env(self, key: str) -> str | None: ...

    async def set_env(self, key: str, value: str) -> None: ...

    async def update_json_mapping_entry(self, key: str, entry_key: str, value: str | None) -> None: ...

    async def update_json_string_set_member(self, key: str, member: str, *, enabled: bool) -> None: ...


class ChannelRoleManager:
    """チャンネルごとのChatbot役割を永続化します。"""

    def __init__(self, environment_repository: DatabaseEnvironmentRepositoryProtocol) -> None:
        self._environment_repository = environment_repository

    async def get_role(self, channel_id: int) -> ChannelRole:
        """チャンネル自体の役割を取得し、未設定なら既定値を返します。"""
        roles = await self._load_roles()
        configured_role = roles.get(str(channel_id))
        if configured_role is not None:
            try:
                return ChannelRole(configured_role)
            except ValueError:
                logger.warning(
                    "Unknown chatbot channel role (channel_id=%s, role=%r)",
                    channel_id,
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
        await self._environment_repository.update_json_mapping_entry(CHANNEL_ROLES_KEY, str(channel_id), role.value)

    async def clear_role(self, channel_id: int) -> None:
        """チャンネル固有の役割を削除し、既定値へ戻します。"""
        await self._environment_repository.update_json_mapping_entry(CHANNEL_ROLES_KEY, str(channel_id), None)

    async def _load_roles(self) -> dict[str, str]:
        """保存済み設定を読み込み、壊れている場合は空の設定として扱います。"""
        serialized_roles = await self._environment_repository.get_env(CHANNEL_ROLES_KEY)
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
