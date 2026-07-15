from enum import StrEnum

from pydantic import BaseModel


class ResponseMode(StrEnum):
    """要否判定後に高品質モデルへ許可する応答形式。"""

    NONE = "none"
    REACTION = "reaction"
    TEXT = "text"


class CooldownStage(StrEnum):
    """最後のBot反応からの経過時間に応じた自発反応の抑制度。"""

    RECENT = "recent"
    RECOVERING = "recovering"
    READY = "ready"


class ResponseJudgment(BaseModel):
    """安価なモデルによる応答形式の判定。"""

    response_mode: ResponseMode
