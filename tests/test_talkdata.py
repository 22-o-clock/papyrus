import unittest

from cogs.talkdata.database import TALKDATA_SCHEMA, TALKDATA_TEST_SCHEMA, get_talkdata_schema
from cogs.talkdata.talkdata import format_upsert_result
from core.runtime_environment import BotEnvironment


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


class TalkDataSchemaTest(unittest.TestCase):
    def test_uses_production_schema_in_production(self) -> None:
        if get_talkdata_schema(BotEnvironment.PRODUCTION) != TALKDATA_SCHEMA:
            self.fail("本番環境でTalkData本番スキーマが選択されていません")

    def test_uses_test_schema_in_debug(self) -> None:
        if get_talkdata_schema(BotEnvironment.DEBUG) != TALKDATA_TEST_SCHEMA:
            self.fail("デバッグ環境でTalkDataテストスキーマが選択されていません")
