class BotError(Exception):
    """パッケージ独自の例外の基底クラス"""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class EnvironVarNotFoundError(BotError):
    """必要な環境変数が設定されていない場合の例外"""


class VoiceFetchFailedError(BotError):
    """ボイス情報の取得に失敗した場合の例外"""


class PathNotExistsError(BotError):
    """パスが存在しない場合の例外"""


class CharacterNotExistsError(BotError):
    """キャラクターが存在しない場合の例外"""


class ArgumentError(BotError):
    """適切な実引数が渡されなかった場合の例外"""


class MissingRequiredRoleError(BotError):
    """必要なロールを持っていない場合の例外"""


class HandledError(BotError):
    """動作を中断するために送出する無視して構わない例外"""
