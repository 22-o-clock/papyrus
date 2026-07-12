class BotException(Exception):
    """パッケージ独自の例外の基底クラス"""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class EnvironVarNotFound(BotException):
    """必要な環境変数が設定されていない場合の例外"""


class VoiceFetchFailed(BotException):
    """ボイス情報の取得に失敗した場合の例外"""


class PathNotExists(BotException):
    """パスが存在しない場合の例外"""


class CharacterNotExists(BotException):
    """キャラクターが存在しない場合の例外"""


class ArgumentError(BotException):
    """適切な実引数が渡されなかった場合の例外"""


class MissingRequiredRole(BotException):
    """必要なロールを持っていない場合の例外"""


class HandledError(BotException):
    """動作を中断するために送出する無視して構わない例外"""
