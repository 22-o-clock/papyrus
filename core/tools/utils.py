from discord import Interaction, Member, StageChannel, TextChannel, Thread, User, VoiceChannel, VoiceClient, VoiceState
from discord.ext import commands


def parse_comma_separated_values(raw: str | None) -> list[str]:
    """カンマ区切りの設定値を空白と空要素を除いて分割する。"""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


async def fetch_text_channel(bot: commands.Bot, channel_id: int) -> TextChannel | Thread:
    channel = await bot.fetch_channel(channel_id)

    if isinstance(channel, (Thread, TextChannel)):
        return channel

    error_message = f"Unexpected type of bot.fetch_channel({channel_id}): expected Thread or TextChannel, got {type(channel)}."
    raise TypeError(error_message)


def get_voice_client_from_author(author: User | Member) -> VoiceClient:
    if isinstance(author, User):
        error_message = f"type(author) is {type(author)}"
        raise TypeError(error_message)
    if not isinstance(author.guild.voice_client, VoiceClient):
        error_message = "ctx.guild.voice_client is not VoiceClient"
        raise TypeError(error_message)

    return author.guild.voice_client


def get_voice_client_from_ctx(interaction: Interaction) -> VoiceClient:
    if interaction.guild is None or interaction.guild.voice_client is None:
        error_message = "interaction.guild.voice_client is None"
        raise TypeError(error_message)
    if not isinstance(interaction.guild.voice_client, VoiceClient):
        error_message = "interaction.guild.voice_client is not VoiceClient"
        raise TypeError(error_message)
    return interaction.guild.voice_client


def get_voice_channel_from_author(author: User | Member) -> VoiceChannel | StageChannel:
    if isinstance(author, User):
        error_message = f"type of ctx.author is {type(author)}"
        raise TypeError(error_message)
    if not isinstance(author.voice, VoiceState):
        error_message = f"type of ctx.author.voice is {type(author.voice)}"
        raise TypeError(error_message)
    if author.voice.channel is None:
        error_message = f"ctx.author.voice.channel is {author.voice.channel}"
        raise TypeError(error_message)
    return author.voice.channel


def get_voice_channel_from_ctx(interaction: Interaction) -> VoiceChannel | StageChannel:
    return get_voice_channel_from_author(interaction.user)
