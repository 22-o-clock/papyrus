import discord
from discord import Message

from cogs.chatbot.constants import DISCORD_RESPONSE_CHUNK_LENGTH


def split_discord_response(content: str, maximum_length: int = DISCORD_RESPONSE_CHUNK_LENGTH) -> list[str]:
    """応答を改行位置優先でDiscordへ送信可能な長さに分割します。"""
    if maximum_length < 1:
        error_message = "maximum_length must be greater than zero"
        raise ValueError(error_message)
    if not content:
        return []
    chunks: list[str] = []
    remaining = content
    while len(remaining) > maximum_length:
        split_at = remaining.rfind("\n", 0, maximum_length + 1)
        split_at = maximum_length if split_at <= 0 else split_at + 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_split_response(channel: discord.abc.Messageable, content: str) -> None:
    """長文応答を分割し、同じチャンネルへ順番に送信します。"""
    for chunk in split_discord_response(content):
        await channel.send(chunk, suppress_embeds=True)


async def reply_with_split_response(target: discord.PartialMessage | Message, content: str) -> None:
    """長文応答を分割し、先行メッセージへの返信として連続送信します。"""
    reply_target = target
    for chunk in split_discord_response(content):
        reply_target = await reply_target.reply(chunk, suppress_embeds=True)
