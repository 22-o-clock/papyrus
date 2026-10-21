DEFAULT_MODEL = "gpt-6-luna"
CUSTOM_PROFILE_MODELS = {"system_default", DEFAULT_MODEL, "gpt-6.1-sol"}
LEGACY_MODEL_ALIASES = {
    "gpt-5.6-luna": DEFAULT_MODEL,
    "gpt-5.6-terra": "gpt-6.1-sol",
}


def current_profile_model(model: str) -> str:
    """保存済みの旧モデル指定を現在の同用途モデルへ解決します。"""
    return LEGACY_MODEL_ALIASES.get(model, model)


def resolve_chatbot_model(model: str) -> str:
    """システム既定と旧モデル指定からAPIに渡すモデルを返します。"""
    return DEFAULT_MODEL if model == "system_default" else current_profile_model(model)
