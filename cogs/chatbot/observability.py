from logging import getLogger

logger = getLogger(__name__)


def log_chatbot_api_call(operation: str, model: str, *, item_count: int = 1) -> None:
    """長期テストでAPI呼び出し回数を集計できる構造化ログを残します。"""
    logger.info(
        "Chatbot API call (operation=%s, model=%s, item_count=%s)",
        operation,
        model,
        item_count,
    )
