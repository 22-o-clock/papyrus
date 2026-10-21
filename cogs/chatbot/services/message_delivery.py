from collections.abc import Sequence

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


async def send_split_response(channel: discord.abc.Messageable, content: str) -> list[Message]:
    """長文応答を分割し、同じチャンネルへ順番に送信します。"""
    return [await channel.send(chunk, suppress_embeds=True) for chunk in split_discord_response(content)]


async def reply_with_split_response(
    target: discord.PartialMessage | Message, content: str, *, files: Sequence[discord.File] = ()
) -> list[Message]:
    """本文と添付をまとめ、上限を超えた分だけ連続返信します。

    Args:
        target: 最初の返信先。
        content: 送信する本文。
        files: 添付ファイル。送信後のクローズは呼び出し側が行います。

    Returns:
        送信したメッセージを順番に返します。

    """
    chunks = split_discord_response(content)
    file_batches = [list(files[index : index + 10]) for index in range(0, len(files), 10)]
    reply_target = target
    sent_messages: list[Message] = []
    for index in range(max(len(chunks), len(file_batches))):
        chunk = chunks[index] if index < len(chunks) else None
        attachments = file_batches[index] if index < len(file_batches) else []
        reply_target = await reply_target.reply(
            chunk, files=attachments, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none()
        )
        sent_messages.append(reply_target)
    return sent_messages
