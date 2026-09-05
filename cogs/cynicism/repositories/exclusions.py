"""冷笑ランキングから除外するチャンネルの永続化。"""

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from cogs.cynicism.database import CynicismDatabase, CynicismExcludedChannel


@dataclass(frozen=True, slots=True)
class ExcludedChannel:
    """削除・アーカイブ後も一覧と解除に使える、除外対象のIDと保存時の名前。"""

    channel_id: int
    name: str


class CynicismExclusionRepository:
    """除外設定を実行環境別に保存し、サーバー単位で管理する。"""

    def __init__(self, database: CynicismDatabase) -> None:
        """除外設定を保存する環境別DB接続を保持する。"""
        self._database = database

    async def exclude(self, guild_id: int, channel_id: int, name: str) -> None:
        """対象を除外する。登録済みなら表示名だけ更新する。"""
        statement = insert(CynicismExcludedChannel).values(guild_id=guild_id, channel_id=channel_id, name=name)
        async with self._database.session() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[CynicismExcludedChannel.channel_id], set_={"name": statement.excluded.name}
                )
            )

    async def include(self, guild_id: int, channel_id: int) -> bool:
        """指定サーバーの除外設定を解除し、設定が存在したかを返す。"""
        async with self._database.session() as session:
            removed_id = await session.scalar(
                delete(CynicismExcludedChannel)
                .where(CynicismExcludedChannel.guild_id == guild_id, CynicismExcludedChannel.channel_id == channel_id)
                .returning(CynicismExcludedChannel.channel_id)
            )
            return removed_id is not None

    async def list_excluded(self, guild_id: int) -> list[ExcludedChannel]:
        """明示的な除外設定を、保存した名前・IDの順で返す。"""
        async with self._database.session() as session:
            rows = await session.execute(
                select(CynicismExcludedChannel.channel_id, CynicismExcludedChannel.name)
                .where(CynicismExcludedChannel.guild_id == guild_id)
                .order_by(CynicismExcludedChannel.name, CynicismExcludedChannel.channel_id)
            )
            return [ExcludedChannel(channel_id, name) for channel_id, name in rows.all()]
