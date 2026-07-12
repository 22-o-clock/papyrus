from unittest import TestCase

from cogs.monitor.normalization import FuzzyMatch


class FuzzyMatchTest(TestCase):
    def test_matches_japanese_reading(self) -> None:
        checker = FuzzyMatch("so[uー]*nan+da")
        if not checker.is_match("そうなんだ"):
            raise AssertionError

    def test_does_not_match_unrelated_text(self) -> None:
        checker = FuzzyMatch("so[uー]*nan+da")
        if checker.is_match("こんにちは"):
            raise AssertionError
