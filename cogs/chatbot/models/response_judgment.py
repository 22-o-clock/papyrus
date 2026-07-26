from enum import StrEnum

from pydantic import BaseModel


class ResponseMode(StrEnum):
    """要否判定後に高品質モデルへ許可する応答形式。"""

    NONE = "none"
    REACTION = "reaction"
    TEXT = "text"


class ResponseJudgment(BaseModel):
    """安価なモデルによる応答形式の判定。"""

    response_mode: ResponseMode
