import unittest

from cogs.chatbot.database import (
    determine_member_alias_status,
    find_user_ids_by_member_alias,
    normalize_member_alias,
)


class MemberAliasTest(unittest.TestCase):
    def test_normalize_member_alias_ignores_case_and_extra_spaces(self) -> None:
        if normalize_member_alias("  Te St User  ") != "te st user":
            self.fail("別名の大文字小文字と余分な空白が正規化されません")

    def test_unique_alias_is_active(self) -> None:
        if determine_member_alias_status({123}) != "active":
            self.fail("一人だけを指す別名が有効になりません")

    def test_normalize_member_alias_removes_common_honorific(self) -> None:
        if normalize_member_alias("テストユーザーさん") != "テストユーザー":
            self.fail("別名から一般的な敬称が除かれません")

    def test_alias_shared_by_multiple_members_is_ambiguous(self) -> None:
        if determine_member_alias_status({123, 456}) != "ambiguous":
            self.fail("複数人を指す別名が曖昧になりません")

    def test_active_alias_resolves_name_in_message(self) -> None:
        aliases = {"てすたろう": 123456789}

        if find_user_ids_by_member_alias("てすたろうさんは英語が得意です", aliases) != {123456789}:
            self.fail("会話中の有効な別名から対象メンバーを解決できません")

    def test_unmentioned_alias_does_not_add_member(self) -> None:
        aliases = {"てすたろう": 123456789}

        if find_user_ids_by_member_alias("英語が得意な人は誰ですか", aliases):
            self.fail("会話にない別名からメンバーが追加されました")


if __name__ == "__main__":
    unittest.main()
