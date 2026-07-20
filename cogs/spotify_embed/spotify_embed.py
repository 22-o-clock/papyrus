import asyncio
import json
import re
from dataclasses import dataclass
from logging import getLogger
from urllib.parse import urlparse

import discord
from aiohttp import ClientError, ClientSession, ClientTimeout
from discord import Message
from discord.ext import commands

logger = getLogger(__name__)

SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"
SPOTIFY_GREEN = 0x1DB954
EMBED_WAIT_SECONDS = 4.0
MAX_SPOTIFY_LINKS = 5
SPOTIFY_ENTITY_LABELS = {
    "track": "Track",
    "album": "Album",
    "playlist": "Playlist",
    "artist": "Artist",
    "show": "Show",
    "episode": "Episode",
}
SPOTIFY_URL_PATTERN = re.compile(
    r"https?://(?:"
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:track|album|playlist|artist|show|episode)/[A-Za-z0-9]+"
    r"|spotify\.link/[A-Za-z0-9]+"
    r")(?:\?[^\s<>]*)?",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}、。！？」』】" + chr(0xFF09)
NEXT_DATA_PATTERN = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class SpotifyOEmbed:
    title: str
    thumbnail_url: str | None
    artist_names: tuple[str, ...] = ()
    iframe_url: str | None = None


def extract_spotify_urls(content: str) -> list[str]:
    """投稿から対応するSpotify URLを出現順かつ重複なしで抽出する。"""
    urls: list[str] = []
    for match in SPOTIFY_URL_PATTERN.finditer(content):
        url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        if url not in urls:
            urls.append(url)
        if len(urls) == MAX_SPOTIFY_LINKS:
            break
    return urls


def has_spotify_embed(embeds: list[discord.Embed]) -> bool:
    """Discordが生成したSpotify埋め込みが含まれるか判定する。"""
    for embed in embeds:
        provider_name = embed.provider.name if embed.provider else None
        if provider_name and provider_name.casefold() == "spotify":
            return True
        if embed.url and urlparse(embed.url).hostname in {"open.spotify.com", "spotify.link"}:
            return True
    return False


def spotify_entity_label(url: str) -> str | None:
    """URLからSpotifyコンテンツ種別の表示名を得る。"""
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if path_parts and path_parts[0].startswith("intl-"):
        path_parts = path_parts[1:]
    if not path_parts:
        return None
    return SPOTIFY_ENTITY_LABELS.get(path_parts[0].casefold())


def parse_spotify_oembed(payload: object) -> SpotifyOEmbed:
    """oEmbed応答からカード表示に必要な値だけを検証して取り出す。"""
    if not isinstance(payload, dict):
        error_message = "Spotify oEmbed response must be an object"
        raise TypeError(error_message)

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        error_message = "Spotify oEmbed response does not contain a title"
        raise ValueError(error_message)

    thumbnail_url = payload.get("thumbnail_url")
    if thumbnail_url is not None and not isinstance(thumbnail_url, str):
        error_message = "Spotify oEmbed thumbnail_url must be a string"
        raise ValueError(error_message)

    iframe_url = payload.get("iframe_url")
    if iframe_url is not None and not isinstance(iframe_url, str):
        error_message = "Spotify oEmbed iframe_url must be a string"
        raise ValueError(error_message)
    return SpotifyOEmbed(title=title.strip(), thumbnail_url=thumbnail_url, iframe_url=iframe_url)


def parse_spotify_embed_artists(content: str) -> tuple[str, ...]:
    """Spotify公式埋め込みページの公開データからアーティスト名を取り出す。"""
    match = NEXT_DATA_PATTERN.search(content)
    if match is None:
        return ()
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()

    props = payload.get("props")
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    state = page_props.get("state") if isinstance(page_props, dict) else None
    data = state.get("data") if isinstance(state, dict) else None
    entity = data.get("entity") if isinstance(data, dict) else None
    if not isinstance(entity, dict):
        return ()
    artists = entity.get("artists")
    names: list[str] = []
    if isinstance(artists, list):
        for artist in artists:
            name = artist.get("name") if isinstance(artist, dict) else None
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
    elif entity.get("type") == "album":
        subtitle = entity.get("subtitle")
        if isinstance(subtitle, str) and subtitle.strip():
            names.append(subtitle.strip())
    return tuple(names)


def is_spotify_iframe_url(url: str) -> bool:
    """取得先をSpotify公式の埋め込みページだけに限定する。"""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "open.spotify.com" and parsed.path.startswith("/embed/")


async def fetch_spotify_oembed(url: str) -> SpotifyOEmbed:
    """SpotifyのoEmbed APIから公開メタデータを取得する。"""
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(SPOTIFY_OEMBED_URL, params={"url": url}) as response:
            response.raise_for_status()
            metadata = parse_spotify_oembed(await response.json())

        if metadata.iframe_url is None or not is_spotify_iframe_url(metadata.iframe_url):
            return metadata
        try:
            async with session.get(metadata.iframe_url) as response:
                response.raise_for_status()
                artist_names = parse_spotify_embed_artists(await response.text())
        except (ClientError, TimeoutError):
            logger.warning("Failed to fetch Spotify embed artist metadata for %s", url, exc_info=True)
            return metadata
        return SpotifyOEmbed(
            title=metadata.title,
            thumbnail_url=metadata.thumbnail_url,
            artist_names=artist_names,
            iframe_url=metadata.iframe_url,
        )


def build_spotify_fallback(url: str, metadata: SpotifyOEmbed) -> discord.Embed:
    """再生プレイヤーを代替するカードを作成する。"""
    embed = discord.Embed(title=metadata.title, url=url, colour=SPOTIFY_GREEN)
    embed.set_author(name="Spotify")
    if metadata.artist_names:
        embed.description = ", ".join(metadata.artist_names)
    if metadata.thumbnail_url:
        embed.set_thumbnail(url=metadata.thumbnail_url)
    if entity_label := spotify_entity_label(url):
        embed.set_footer(text=f"Spotify {entity_label}")

    return embed


def build_spotify_view(urls: list[str]) -> discord.ui.View:
    """各カードをSpotifyで開くリンクボタンを作成する。"""
    view = discord.ui.View()
    for url in urls:
        view.add_item(discord.ui.Button(label="Open in Spotify", style=discord.ButtonStyle.link, url=url))
    return view


class SpotifyEmbedFallback(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.guild is None or message.author.bot:
            return
        spotify_urls = extract_spotify_urls(message.content)
        if spotify_urls:
            await self._send_fallback_after_wait(message, spotify_urls)

    async def _send_fallback_after_wait(self, message: Message, spotify_urls: list[str]) -> None:
        """Discordの埋め込み生成を待ち、生成されなかった場合だけカードを返信する。"""
        await asyncio.sleep(EMBED_WAIT_SECONDS)
        refreshed_message = await self._refresh_message(message)
        if refreshed_message is None or has_spotify_embed(refreshed_message.embeds):
            return

        fallback_embeds, fallback_urls = await self._build_fallbacks(spotify_urls)
        if fallback_embeds:
            await self._reply_with_fallback(refreshed_message, fallback_embeds, fallback_urls)

    @staticmethod
    async def _refresh_message(message: Message) -> Message | None:
        """非同期で追加された埋め込みを含む最新のメッセージを取得する。"""
        try:
            return await message.channel.fetch_message(message.id)
        except (discord.Forbidden, discord.NotFound):
            return None
        except discord.HTTPException:
            logger.warning("Failed to refresh Discord message %s before Spotify fallback", message.id, exc_info=True)
            return None

    @staticmethod
    async def _build_fallbacks(spotify_urls: list[str]) -> tuple[list[discord.Embed], list[str]]:
        """取得できたSpotifyメタデータだけをカードに変換する。"""
        fallback_embeds: list[discord.Embed] = []
        fallback_urls: list[str] = []
        for url in spotify_urls:
            try:
                metadata = await fetch_spotify_oembed(url)
            except (ClientError, TimeoutError, ValueError):
                logger.warning("Failed to fetch Spotify oEmbed metadata for %s", url, exc_info=True)
                continue
            fallback_embeds.append(build_spotify_fallback(url, metadata))
            fallback_urls.append(url)
        return fallback_embeds, fallback_urls

    @staticmethod
    async def _reply_with_fallback(message: Message, embeds: list[discord.Embed], urls: list[str]) -> None:
        """元投稿へのメンションを発生させずフォールバックを返信する。"""
        try:
            await message.reply(embeds=embeds, view=build_spotify_view(urls), mention_author=False)
        except (discord.Forbidden, discord.NotFound):
            return
        except discord.HTTPException:
            logger.warning("Failed to send Spotify fallback for message %s", message.id, exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SpotifyEmbedFallback(bot))
    logger.debug("%s is added to the bot.", __name__)
