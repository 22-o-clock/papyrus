from unittest import TestCase

from cogs.moving.moving import fetch_message_id_from_url
from core.exception.exception import ArgumentError

MESSAGE_ID = 123456789


class FetchMessageIdFromUrlTest(TestCase):
    def test_extracts_message_id(self) -> None:
        result = fetch_message_id_from_url(f"https://discord.com/channels/1/2/{MESSAGE_ID}")
        if result != MESSAGE_ID:
            raise AssertionError

    def test_rejects_invalid_url(self) -> None:
        try:
            fetch_message_id_from_url("https://example.com/not-a-message")
        except ArgumentError as error:
            if not str(error):
                raise AssertionError from error
            return
        raise AssertionError
