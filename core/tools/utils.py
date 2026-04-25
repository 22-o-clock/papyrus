from discord import TextChannel, Thread, VoiceChannel, StageChannel
from discord import VoiceClient, VoiceState, User, Member
from discord import Interaction
from discord.ext import commands


async def fetch_text_channel(bot: commands.Bot, channel_id: int):
    channel = await bot.fetch_channel(channel_id)

    if isinstance(channel, (Thread, TextChannel)):
        return channel

    else:
        raise TypeError(
            f"Unexpected type of bot.fetch_channel({channel_id}):expected Thread or TextChannel, got {type(channel)}."
        )


def get_voice_client_from_author(author: User | Member) -> VoiceClient:
    if isinstance(author, User):
        raise TypeError(f"type(author) is {type(author)}")
    if not isinstance(author.guild.voice_client, VoiceClient):
        raise TypeError("ctx.guild.voice_client is not VoiceClient")

    return author.guild.voice_client


def get_voice_client_from_ctx(interaction: Interaction) -> VoiceClient:
    if interaction.guild is None or interaction.guild.voice_client is None:
        raise TypeError("interaction.guild.voice_client is None")
    if not isinstance(interaction.guild.voice_client, VoiceClient):
        raise TypeError("interaction.guild.voice_client is not VoiceClient")
    return interaction.guild.voice_client


def get_voice_channel_from_author(author: User | Member) -> VoiceChannel | StageChannel:
    if isinstance(author, User):
        raise TypeError(f"type of ctx.author is {type(author)}")
    if not isinstance(author.voice, VoiceState):
        raise TypeError(f"type of ctx.author.voice is {type(author.voice)}")
    if author.voice.channel is None:
        raise TypeError(f"ctx.author.voice.channel is {author.voice.channel}")
    return author.voice.channel


def get_voice_channel_from_ctx(interaction: Interaction) -> VoiceChannel | StageChannel:
    try:
        vc: VoiceChannel | StageChannel = get_voice_channel_from_author(interaction.user)
    except TypeError as error:
        raise error

    return vc
