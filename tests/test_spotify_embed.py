from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import Message

from cogs.spotify_embed.spotify_embed import (
    SpotifyEmbedFallback,
    SpotifyOEmbed,
    build_spotify_fallback,
    build_spotify_view,
    extract_spotify_urls,
    has_spotify_embed,
    is_spotify_iframe_url,
    parse_spotify_embed_artists,
    parse_spotify_oembed,
    spotify_entity_label,
)

if TYPE_CHECKING:
    from discord.ext import commands


class SpotifyUrlTest(TestCase):
    def test_extracts_supported_content_types_without_duplicates(self) -> None:
        content = (
            "https://open.spotify.com/track/abc?si=one "
            "https://open.spotify.com/album/def。 "
            "https://open.spotify.com/playlist/ghi "
            "https://open.spotify.com/track/abc?si=one "
            "https://spotify.link/short123"
        )

        result = extract_spotify_urls(content)

        if result != [
            "https://open.spotify.com/track/abc?si=one",
            "https://open.spotify.com/album/def",
            "https://open.spotify.com/playlist/ghi",
            "https://spotify.link/short123",
        ]:
            raise AssertionError(result)

    def test_gets_labels_for_localized_urls(self) -> None:
        if spotify_entity_label("https://open.spotify.com/intl-ja/playlist/abc") != "Playlist":
            raise AssertionError
        if spotify_entity_label("https://spotify.link/abc") is not None:
            raise AssertionError

    def test_detects_provider_embed(self) -> None:
        embed = discord.Embed.from_dict({"provider": {"name": "Spotify"}, "type": "link"})

        if not has_spotify_embed([embed]):
            raise AssertionError


class SpotifyOEmbedTest(TestCase):
    def test_parses_track_metadata(self) -> None:
        result = parse_spotify_oembed(
            {
                "title": " Song ",
                "thumbnail_url": "https://i.scdn.co/image/abc",
                "iframe_url": "https://open.spotify.com/embed/track/abc",
            }
        )

        if result != SpotifyOEmbed(
            title="Song",
            thumbnail_url="https://i.scdn.co/image/abc",
            iframe_url="https://open.spotify.com/embed/track/abc",
        ):
            raise AssertionError(result)

    def test_parses_artists_from_embed_page(self) -> None:
        next_data = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"state":{"data":{"entity":{"artists":['
            '{"name":"First Artist"},{"name":"Second Artist"}]}}}}}}'
            "</script>"
        )

        if parse_spotify_embed_artists(next_data) != ("First Artist", "Second Artist"):
            raise AssertionError

    def test_parses_album_artist_from_subtitle(self) -> None:
        next_data = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"state":{"data":{"entity":'
            '{"type":"album","subtitle":"Album Artist"}}}}}}'
            "</script>"
        )

        if parse_spotify_embed_artists(next_data) != ("Album Artist",):
            raise AssertionError

    def test_accepts_only_spotify_embed_iframe_urls(self) -> None:
        if not is_spotify_iframe_url("https://open.spotify.com/embed/album/abc"):
            raise AssertionError
        if is_spotify_iframe_url("https://example.com/embed/album/abc"):
            raise AssertionError

    def test_rejects_missing_title(self) -> None:
        try:
            parse_spotify_oembed({"thumbnail_url": "https://i.scdn.co/image/abc"})
        except ValueError:
            return
        raise AssertionError

    def test_builds_album_card_and_link_button(self) -> None:
        url = "https://open.spotify.com/album/abc"

        embed = build_spotify_fallback(
            url,
            SpotifyOEmbed("Album", "https://i.scdn.co/image/abc", ("First Artist", "Second Artist")),
        )
        view = build_spotify_view([url])

        if (
            embed.title != "Album"
            or embed.url != url
            or embed.description != "First Artist, Second Artist"
            or embed.footer.text != "Spotify Album"
        ):
            raise AssertionError(embed.to_dict())
        button = cast("discord.ui.Button[discord.ui.View]", view.children[0])
        if button.label != "Open in Spotify" or button.url != url:
            raise AssertionError


class SpotifyEmbedFallbackTest(IsolatedAsyncioTestCase):
    @staticmethod
    def _message(*, refreshed_embeds: list[discord.Embed] | None = None) -> tuple[Message, AsyncMock]:
        reply = AsyncMock()
        refreshed = SimpleNamespace(embeds=refreshed_embeds or [], reply=reply)
        fetch_message = AsyncMock(return_value=refreshed)
        message = cast(
            "Message",
            SimpleNamespace(
                id=123,
                guild=SimpleNamespace(id=1),
                author=SimpleNamespace(bot=False),
                content="https://open.spotify.com/playlist/abc",
                channel=SimpleNamespace(fetch_message=fetch_message),
            ),
        )
        return message, reply

    @patch("cogs.spotify_embed.spotify_embed.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.spotify_embed.spotify_embed.fetch_spotify_oembed", new_callable=AsyncMock)
    async def test_does_not_reply_when_discord_created_spotify_embed(
        self,
        fetch_oembed: AsyncMock,
        sleep: AsyncMock,
    ) -> None:
        native_embed = discord.Embed.from_dict({"provider": {"name": "Spotify"}, "type": "rich"})
        message, reply = self._message(refreshed_embeds=[native_embed])
        cog = SpotifyEmbedFallback(cast("commands.Bot", Mock()))

        await cog.on_message(message)

        sleep.assert_awaited_once()
        fetch_oembed.assert_not_awaited()
        reply.assert_not_awaited()

    @patch("cogs.spotify_embed.spotify_embed.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.spotify_embed.spotify_embed.fetch_spotify_oembed", new_callable=AsyncMock)
    async def test_replies_with_fallback_when_native_embed_is_missing(
        self,
        fetch_oembed: AsyncMock,
        sleep: AsyncMock,
    ) -> None:
        fetch_oembed.return_value = SpotifyOEmbed("Playlist", "https://i.scdn.co/image/abc")
        message, reply = self._message()
        cog = SpotifyEmbedFallback(cast("commands.Bot", Mock()))

        await cog.on_message(message)

        sleep.assert_awaited_once()
        fetch_oembed.assert_awaited_once_with("https://open.spotify.com/playlist/abc")
        reply.assert_awaited_once()
        await_args = reply.await_args
        if await_args is None:
            raise AssertionError
        kwargs = await_args.kwargs
        if kwargs["embeds"][0].title != "Playlist" or kwargs["mention_author"] is not False:
            raise AssertionError(kwargs)
