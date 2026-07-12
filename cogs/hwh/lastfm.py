import os
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import getLogger
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

logger = getLogger(__name__)

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


@dataclass(frozen=True, slots=True)
class Scrobble:
    title: str
    artist: str
    album: str
    time: datetime


def extract_latest_track(payload: dict[str, Any]) -> Scrobble | None:
    """Last.fmレスポンスから再生中ではない最新曲を取り出す。"""
    tracks = payload.get("recenttracks", {}).get("track", [])
    for track in tracks:
        if track.get("@attr", {}).get("nowplaying") == "true":
            continue

        try:
            return Scrobble(
                title=str(track["name"]),
                artist=str(track["artist"]["name"]),
                album=str(track["album"]["#text"]),
                time=datetime.fromtimestamp(int(track["date"]["uts"]), tz=UTC),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Last.fm returned an invalid track entry")
            return None
    return None


async def fetch_latest_track(user: str) -> Scrobble | None:
    """指定ユーザーの最新Scrobbleを取得し、失敗時はNoneを返す。"""
    params = {
        "method": "user.getrecenttracks",
        "limit": 10,
        "user": user,
        "api_key": os.environ["LASTFM_API_KEY"],
        "extended": 1,
        "format": "json",
    }
    try:
        async with (
            ClientSession(timeout=ClientTimeout(total=15)) as session,
            session.get(LASTFM_API_URL, params=params) as response,
        ):
            response.raise_for_status()
            payload: dict[str, Any] = await response.json()
    except (ClientError, TimeoutError, ValueError):
        logger.exception("Failed to fetch Last.fm data for %s", user)
        return None

    return extract_latest_track(payload)
