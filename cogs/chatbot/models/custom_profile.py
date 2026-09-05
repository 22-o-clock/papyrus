from dataclasses import dataclass


@dataclass(frozen=True)
class CustomProfile:
    """1回の明示的な応答生成に適用するカスタムプロファイル。"""

    name: str
    instructions: str
    model: str
    request_message_id: int
    request_content: str
