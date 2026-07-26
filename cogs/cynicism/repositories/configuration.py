"""冷笑ポイントの重みと一時停止状態の永続化。"""

import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from cogs.cynicism.constants import CONFIGURATION_ID, DEFAULT_HUMAN_WEIGHT, DEFAULT_PAPYRUS_WEIGHT
from cogs.cynicism.database import CynicismConfiguration, CynicismDatabase
from cogs.cynicism.models import CynicismSettings, CynicismWeights


class CynicismConfigurationRepository:
    """単一行の運用設定を読み書きする。"""

    def __init__(self, database: CynicismDatabase) -> None:
        self._database = database

    async def get(self) -> CynicismSettings:
        """設定を取得し、未作成なら既定の重みで初期化する。"""
        async with self._database.session() as session:
            statement = (
                insert(CynicismConfiguration)
                .values(
                    id=CONFIGURATION_ID,
                    papyrus_weight=DEFAULT_PAPYRUS_WEIGHT,
                    human_weight=DEFAULT_HUMAN_WEIGHT,
                    is_paused=False,
                    paused_at=None,
                    updated_at=datetime.datetime.now(datetime.UTC),
                )
                .on_conflict_do_nothing(index_elements=[CynicismConfiguration.id])
            )
            await session.execute(statement)
            await session.commit()
            row = (
                await session.execute(select(CynicismConfiguration).where(CynicismConfiguration.id == CONFIGURATION_ID))
            ).scalar_one()
            return _to_settings(row)

    async def set_weights(self, *, papyrus: Decimal | None, human: Decimal | None) -> CynicismSettings:
        """指定された重みだけを更新し、省略された側は据え置く。"""
        current = await self.get()
        updated = {
            "papyrus_weight": current.weights.papyrus if papyrus is None else papyrus,
            "human_weight": current.weights.human if human is None else human,
            "updated_at": datetime.datetime.now(datetime.UTC),
        }
        return await self._update(updated)

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
            await session.commit()
            row = (
                await session.execute(select(CynicismConfiguration).where(CynicismConfiguration.id == CONFIGURATION_ID))
            ).scalar_one()
            return _to_settings(row)


def _to_settings(row: CynicismConfiguration) -> CynicismSettings:
    """ORM行をセッション外で安全に使える値へ変換する。"""
    return CynicismSettings(
        weights=CynicismWeights(papyrus=row.papyrus_weight, human=row.human_weight),
        is_paused=row.is_paused,
        paused_at=row.paused_at,
    )
