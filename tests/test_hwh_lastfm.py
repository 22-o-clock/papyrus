from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from discord import Colour

from cogs.hwh.hwh import Patchwork
from cogs.hwh.lastfm import extract_latest_track
from core.tools.ebd import make_simple_embed
from core.tools.utils import parse_comma_separated_values

if TYPE_CHECKING:
    from discord import Message


class ExtractLatestTrackTest(TestCase):
    def test_skips_now_playing_track(self) -> None:
        payload = {
            "recenttracks": {
                "track": [
                    {
                        "name": "playing",
                        "artist": {"name": "artist"},
                        "album": {"#text": "album"},
                        "@attr": {"nowplaying": "true"},
                    },
                    {
                        "name": "completed",
                        "artist": {"name": "artist"},
                        "album": {"#text": "album"},
                        "date": {"uts": "1704067200"},
                    },
                ]
            }
        }

        result = extract_latest_track(payload)

        if result is None:
            raise AssertionError
        if result.title != "completed":
            raise AssertionError
        if result.time != datetime(2024, 1, 1, tzinfo=UTC):
            raise AssertionError

    def test_returns_none_for_empty_track_list(self) -> None:
        if extract_latest_track({"recenttracks": {"track": []}}) is not None:
            raise AssertionError


class ParseCommaSeparatedValuesTest(TestCase):
    def test_strips_whitespace_and_empty_values(self) -> None:
        result = parse_comma_separated_values(" first, second ,,third ")
        if result != ["first", "second", "third"]:
            raise AssertionError


class StayFocusedMessageTest(IsolatedAsyncioTestCase):
    async def test_deletes_message_without_arguments(self) -> None:
        cog = object.__new__(Patchwork)
        cog.prohibited_users = {1: {2}}
        delete = AsyncMock()
        message = cast(
            "Message",
            SimpleNamespace(
                guild=SimpleNamespace(id=1),
                author=SimpleNamespace(id=2),
                delete=delete,
            ),
        )

        await cog.on_message(message)

        if delete.await_count != 1:
            raise AssertionError
        await_args = delete.await_args
        if await_args is None or await_args.kwargs:
            raise AssertionError


class MakeSimpleEmbedTest(TestCase):
    def test_sets_url(self) -> None:
        embed = make_simple_embed(Colour.teal(), "title", "description", url="https://example.com/event")
        if embed.url != "https://example.com/event":
            raise AssertionError
