import unittest

from cogs.chatbot.services.custom_profile_parser import (
    CustomProfileDirectiveError,
    InvalidCustomProfileDirectiveError,
    parse_custom_profile_directive,
)


class CustomProfileParserTest(unittest.TestCase):
    def test_parses_first_directive_after_direct_mention(self) -> None:
        parsed = parse_custom_profile_directive(
            "<@123> OPTION ReS-Ba\n本文です",
            bot_user_id=123,
            directly_mentioned=True,
        )

        if parsed is None or parsed.name != "res-ba" or parsed.content != "本文です":
            self.fail("直接メンション後のoption指定を正規化して分離できません")

    def test_parses_inline_content_after_profile_name(self) -> None:
        parsed = parse_custom_profile_directive(
            "<@123> option poet いつまでも終わらない仕事について一句読んで",
            bot_user_id=123,
            directly_mentioned=True,
        )

        if parsed is None or parsed.name != "poet":
            self.fail("同じ行のoption指定を抽出できません")
        if parsed.content != "いつまでも終わらない仕事について一句読んで":
            self.fail("option名に続く同じ行の本文を分離できません")

    def test_parses_directive_after_bot_role_mention(self) -> None:
        parsed = parse_custom_profile_directive(
            "<@&456> option poet\n本文です",
            bot_user_id=123,
            directly_mentioned=True,
            bot_role_ids={456},
        )

        if parsed is None or parsed.name != "poet" or parsed.content != "本文です":
            self.fail("Botの同名ロールへのメンション後にあるoption指定を分離できません")

    def test_ignores_option_without_direct_mention(self) -> None:
        parsed = parse_custom_profile_directive(
            "option poet\n本文です",
            bot_user_id=123,
            directly_mentioned=False,
        )

        if parsed is not None:
            self.fail("直接メンションのないoption指定を誤検出しています")

    def test_ignores_option_in_message_body(self) -> None:
        parsed = parse_custom_profile_directive(
            "<@123> 普通の本文\noption poet",
            bot_user_id=123,
            directly_mentioned=True,
        )

        if parsed is not None:
            self.fail("本文途中のoptionを誤検出しています")

    def test_rejects_missing_request_content(self) -> None:
        try:
            parse_custom_profile_directive(
                "<@123> option poet",
                bot_user_id=123,
                directly_mentioned=True,
            )
        except InvalidCustomProfileDirectiveError as error:
            if error.reason is not CustomProfileDirectiveError.MISSING_CONTENT:
                self.fail("本文のないoption指定が適切な理由で拒否されていません")
            return

        self.fail("本文のないoption指定が拒否されていません")

    def test_rejects_invalid_profile_name(self) -> None:
        try:
            parse_custom_profile_directive(
                "<@123> option 日本語\n本文です",
                bot_user_id=123,
                directly_mentioned=True,
            )
        except InvalidCustomProfileDirectiveError as error:
            if error.reason is not CustomProfileDirectiveError.INVALID_NAME:
                self.fail("不正なプロファイル名が適切な理由で拒否されていません")
            return

        self.fail("不正なプロファイル名が拒否されていません")
