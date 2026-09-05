"""冷笑ポイントの一時停止状態の永続化。"""

import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from cogs.cynicism.constants import CONFIGURATION_ID
from cogs.cynicism.database import CynicismConfiguration, CynicismDatabase
from cogs.cynicism.models import CynicismSettings


class CynicismConfigurationRepository:
    """単一行の運用設定を読み書きする。"""

    def __init__(self, database: CynicismDatabase) -> None:
        """運用設定の読み書きに使う環境別DB接続を保持する。"""
        self._database = database

    async def get(self) -> CynicismSettings:
        """設定を取得し、未作成なら稼働状態で初期化する。"""
        async with self._database.session() as session:
            statement = (
                insert(CynicismConfiguration)
                .values(
                    id=CONFIGURATION_ID,
                    is_paused=False,
                    paused_at=None,
                    updated_at=datetime.datetime.now(datetime.UTC),
                )
                .on_conflict_do_nothing(index_elements=[CynicismConfiguration.id])
            )
            await session.execute(statement)
            row = (
                await session.execute(select(CynicismConfiguration).where(CynicismConfiguration.id == CONFIGURATION_ID))
            ).scalar_one()
            return _to_settings(row)

    async def set_paused(self, *, paused: bool, now: datetime.datetime) -> CynicismSettings:
        """集計の一時停止状態を切り替える。"""
        return await self._update(
            {
                "is_paused": paused,
                "paused_at": now if paused else None,
                "updated_at": now,
            }
        )

    async def _update(self, values: dict[str, object]) -> CynicismSettings:
        """設定行を更新し、更新後の値を返す。"""
        await self.get()
        async with self._database.session() as session:
            await session.execute(
                update(CynicismConfiguration).where(CynicismConfiguration.id == CONFIGURATION_ID).values(**values)
            )
            row = (
                await session.execute(select(CynicismConfiguration).where(CynicismConfiguration.id == CONFIGURATION_ID))
            ).scalar_one()
            return _to_settings(row)


def _to_settings(row: CynicismConfiguration) -> CynicismSettings:
    """ORM行をセッション外で安全に使える値へ変換する。"""
    return CynicismSettings(
        is_paused=row.is_paused,
        paused_at=row.paused_at,
    )
