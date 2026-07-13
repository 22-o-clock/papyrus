from logging import getLogger

logger = getLogger(__name__)


def log_chatbot_api_call(
    operation: str,
    model: str,
    *,
    item_count: int = 1,
    custom_profile: str | None = None,
) -> None:
    """長期テストでAPI呼び出し回数を集計できる構造化ログを残します。"""
    logger.info(
        "Chatbot API call (operation=%s, model=%s, item_count=%s, custom_profile=%s)",
        operation,
        model,
        item_count,
        custom_profile,
    )
