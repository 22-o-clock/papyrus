"""投票の作成時に入力された質問と選択肢を検証する。"""

from cogs.poll.constants import MAX_OPTION_LENGTH, MAX_OPTIONS, MAX_QUESTION_LENGTH, MIN_OPTIONS
from core.exception import ArgumentError


def parse_question(value: str) -> str:
    """前後の空白を除いた質問を返す。

    Raises:
        ArgumentError: 空の場合、または長すぎる場合。

    """
    question = value.strip()
    if not question or len(question) > MAX_QUESTION_LENGTH:
        message = f"質問は1〜{MAX_QUESTION_LENGTH}文字で入力してください... 💦"
        raise ArgumentError(message)
    return question


def parse_options(value: str) -> tuple[str, ...]:
    """1行に1つずつ入力された選択肢を、空行を除いて返す。

    Raises:
        ArgumentError: 個数、文字数、重複のいずれかが条件を満たさない場合。

    """
    options = tuple(line.strip() for line in value.splitlines() if line.strip())
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        message = f"選択肢は1行に1つずつ、{MIN_OPTIONS}〜{MAX_OPTIONS}個入力してください... 💦"
        raise ArgumentError(message)
    if any(len(option) > MAX_OPTION_LENGTH for option in options):
        message = f"選択肢は1つあたり{MAX_OPTION_LENGTH}文字以内で入力してください... 💦"
        raise ArgumentError(message)
    if len(set(options)) != len(options):
        message = "同じ選択肢が複数あります... 💦"
        raise ArgumentError(message)
    return options
