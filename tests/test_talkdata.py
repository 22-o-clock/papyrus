import unittest

from cogs.talkdata.talkdata import format_upsert_result


class FormatUpsertResultTest(unittest.TestCase):
    def test_success_only_omits_error_section(self) -> None:
        result = format_upsert_result(["alpha"], [], "メンバー")

        if result != "以下のメンバーを登録しました\nalpha":
            raise AssertionError

    def test_errors_only_omits_success_section(self) -> None:
        result = format_upsert_result([], ["beta"], "チャンネル")

        if result != "以下のチャンネルは登録できませんでした\nbeta":
            raise AssertionError

    def test_success_and_errors_include_both_sections(self) -> None:
        result = format_upsert_result(["alpha"], ["beta"], "メンバー")

        expected = "以下のメンバーを登録しました\nalpha\n以下のメンバーは登録できませんでした\nbeta"
        if result != expected:
            raise AssertionError
