from dataclasses import dataclass


@dataclass(frozen=True)
class CustomProfile:
    """1回の明示的な応答生成に適用するカスタムプロファイル。"""

    name: str
    instructions: str
    model: str
    request_message_id: int
    request_content: str


@dataclass(frozen=True)
class ResponseRequestOptions:
    """応答キューへ渡す生成要求固有の設定。"""

    is_explicit_call: bool
    custom_profile: CustomProfile | None = None
