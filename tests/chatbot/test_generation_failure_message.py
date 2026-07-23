import unittest
from typing import cast
from unittest.mock import Mock

from openai import RateLimitError

from cogs.chatbot.use_cases.conversation import (
    GENERIC_GENERATION_FAILURE_MESSAGE,
    INSUFFICIENT_QUOTA_MESSAGE,
    get_generation_failure_message,
)


class GenerationFailureMessageTest(unittest.TestCase):
    def test_reports_insufficient_quota(self) -> None:
        error = Mock(spec=RateLimitError)
        error.code = "insufficient_quota"

        result = get_generation_failure_message(cast("RateLimitError", error))

        if result != INSUFFICIENT_QUOTA_MESSAGE:
            self.fail("OpenAI APIの利用クォータ不足を利用者向けメッセージで案内していません")

    def test_preserves_generic_message_for_other_rate_limits(self) -> None:
        error = Mock(spec=RateLimitError)
        error.code = "rate_limit_exceeded"

        result = get_generation_failure_message(cast("RateLimitError", error))

        if result != GENERIC_GENERATION_FAILURE_MESSAGE:
            self.fail("通常のレート制限にクォータ不足の案内を表示しています")

    def test_preserves_generic_message_for_unrelated_errors(self) -> None:
        result = get_generation_failure_message(RuntimeError("unexpected failure"))

        if result != GENERIC_GENERATION_FAILURE_MESSAGE:
            self.fail("OpenAI API以外の生成失敗で既存の案内を維持していません")
