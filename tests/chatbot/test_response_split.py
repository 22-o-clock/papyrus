import unittest

from cogs.chatbot.services.message_delivery import split_discord_response


class SplitDiscordResponseTest(unittest.TestCase):
    def test_short_response_is_not_split(self) -> None:
        chunks = split_discord_response("short response", maximum_length=20)

        if chunks != ["short response"]:
            raise AssertionError

    def test_response_prefers_newline_boundary(self) -> None:
        content = "first line\nsecond line"

        chunks = split_discord_response(content, maximum_length=12)

        if chunks != ["first line\n", "second line"]:
            raise AssertionError

    def test_long_line_is_split_without_losing_content(self) -> None:
        content = "abcdefghij"

        chunks = split_discord_response(content, maximum_length=4)

        if chunks != ["abcd", "efgh", "ij"] or "".join(chunks) != content:
            raise AssertionError

    def test_empty_response_has_no_chunks(self) -> None:
        if split_discord_response(""):
            raise AssertionError
