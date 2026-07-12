import asyncio
import io
import os
import re
from logging import getLogger
from re import Pattern

from aiohttp import ClientSession
from discord import (
    ClientException,
    FFmpegPCMAudio,
    Interaction,
    Message,
    StageChannel,
    VoiceChannel,
    VoiceClient,
    app_commands,
)
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.tools.utils import get_voice_channel_from_ctx, get_voice_client_from_author, get_voice_client_from_ctx

from .database import VoiceVoxDatabase

logger = getLogger(__name__)

DEFAULT_SPEAKER = 8
MESSAGE_READ_MAX_LENGTH = 50
HTTP_OK = 200

URL_PATTERN: Pattern[str] = re.compile(r"https?://")


class Voicevox(commands.Cog):
    def __init__(self, bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot: commands.Bot = bot
        self.db = VoiceVoxDatabase(session_factory)
        self.lock = asyncio.Lock()
        self.voicevox_url = os.environ["VOICEVOX_URL"]
        self.message_channel = os.environ["LISTEN_ONLY_MEMBER"]
        self.character_for_member: dict[int, int] = {}
        self.speakers_cache: dict[int, str] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.character_for_member = await self.db.get_speakers()
        self.speakers_cache = await self.get_speakers()

        logger.info("Voicevox cog is ready.")

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:  # noqa: PLR0911  # 読み上げ対象外の早期リターンが多く、規模上やむを得ない
        if message.author.bot:
            return
        if str(message.channel.id) != self.message_channel:
            return
        if self.does_include_url(message):
            return

        if message.guild is None or message.guild.voice_client is None:
            return

        try:
            vc: VoiceClient = get_voice_client_from_author(message.author)
        except TypeError as error:
            logger.warning("Error occurred while connecting to voice channel: %s", error)
            return

        speaker_id = self.character_for_member.get(message.author.id, DEFAULT_SPEAKER)

        async with ClientSession() as session:
            async with session.post(
                f"{self.voicevox_url}/audio_query",
                params={"text": message.content, "speaker": speaker_id},
            ) as response:
                if response.status != HTTP_OK:
                    logger.error("Failed to get audio query: %s", response.status)
                    return
                audio_query = await response.json()

            async with session.post(
                f"{self.voicevox_url}/synthesis",
                params={"speaker": speaker_id},
                json=audio_query,
            ) as response:
                if response.status != HTTP_OK:
                    logger.error("Failed to synthesize audio: %s", response.status)
                    return
                audio_data: bytes = await response.read()

        async with self.lock:
            done = asyncio.Event()
            vc.play(FFmpegPCMAudio(io.BytesIO(audio_data), pipe=True), after=lambda _: done.set())
            await done.wait()

    @app_commands.command(description="botが再生している音声を一時停止します")
    async def pause(self, interaction: Interaction) -> None:
        try:
            vc: VoiceClient = get_voice_client_from_ctx(interaction)
        except TypeError:
            await interaction.response.send_message("bot is not in VC")
            return
        vc.pause()
        await interaction.response.send_message("paused")

    @app_commands.command(description="botを自身のいるボイスチャンネルに接続させます")
    async def connect_vc(self, interaction: Interaction) -> None:
        try:
            ch: VoiceChannel | StageChannel = get_voice_channel_from_ctx(interaction)
        except TypeError:
            await interaction.response.send_message("you are not in VC")
            return

        try:
            await ch.connect()
        except ClientException:
            await interaction.response.send_message("already connected")
            return
        await interaction.response.send_message("connected")

    @app_commands.command(description="botをボイスチャンネルから切断します")
    async def disconnect_vc(self, interaction: Interaction) -> None:
        try:
            vc: VoiceClient = get_voice_client_from_ctx(interaction)
        except TypeError:
            await interaction.response.send_message("bot is not in VC")
            return
        await vc.disconnect()
        await interaction.response.send_message("disconnected")

    @app_commands.command(description="喋るキャラクターを指定します")
    async def set_speaker(self, interaction: Interaction, speaker: int) -> None:
        if speaker not in self.speakers_cache:
            await interaction.response.send_message("Invalid speaker_id", ephemeral=True)
            return

        self.character_for_member[interaction.user.id] = speaker
        await self.db.set_speaker(interaction.user.id, speaker)
        await interaction.response.send_message(f"speaker set to {self.speakers_cache[speaker]}")

    @set_speaker.autocomplete("speaker")
    async def speaker_autocomplete(self, _: Interaction, current: str) -> list[app_commands.Choice[int]]:
        return [
            app_commands.Choice(name=name, value=sid)
            for sid, name in self.speakers_cache.items()
            if current.lower() in name.lower()
        ][:25]

    @app_commands.command(description="現在の喋るキャラクターを表示します")
    async def show_current_speaker(self, interaction: Interaction) -> None:
        speaker_id = self.character_for_member.get(interaction.user.id, DEFAULT_SPEAKER)
        speaker_name = self.speakers_cache.get(speaker_id, "Unknown Speaker")
        await interaction.response.send_message(f"現在のキャラクター: {speaker_name} (ID: {speaker_id})", ephemeral=True)

    async def get_speakers(self) -> dict[int, str]:
        async with ClientSession() as session, session.get(f"{self.voicevox_url}/speakers") as response:
            if response.status != HTTP_OK:
                logger.error("Failed to get speakers: %s", response.status)
                return {}
            speakers = await response.json()

        response = {}

        for speaker in speakers:
            name: str = speaker["name"]
            for style in speaker["styles"]:
                response[style["id"]] = f"{name} - {style['name']}"

        return response

    def does_include_url(self, message: Message) -> bool:
        return bool(URL_PATTERN.search(message.content))


async def setup(bot: commands.Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await bot.add_cog(Voicevox(bot, session_factory))
