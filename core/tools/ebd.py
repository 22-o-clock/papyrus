from datetime import datetime, timedelta, timezone
from traceback import format_exception

from discord import Colour, Embed
from discord.ext import commands
from discord.utils import escape_markdown


def make_simple_embed(color: Colour, title: str = "", desc: str = ""):
    """基本的な`Embed`を作成します。`color`は`Colour.from_rgb()`で自由に指定することができます。"""
    return Embed(title=title, color=color, description=desc)


def make_timestamped_embed(color: Colour, title: str = "", desc: str = ""):
    """タイムスタンプの付いた`Embed`を作成します。`color`は`Colour.from_rgb()`で自由に指定することができます。"""
    time = datetime.now(timezone(timedelta(hours=9)))

    return Embed(title=title, color=color, description=desc, timestamp=time)


def add_timestamp_footer(bot: commands.Bot, embed: Embed) -> Embed:
    """フッターとして、現在の時刻と、`bot`のアイコンを追加します。"""
    tz = timezone(timedelta(hours=9))
    stamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")

    embed.set_footer(text=f"occured on {stamp}", icon_url=bot.user.display_avatar.url)
    return embed


def make_error_embed(color: Colour, title: str, error: Exception) -> Embed:
    """タイムスタンプとエラー情報を含んだ`embed`を作成します。フッターは別に付ける必要があります。"""
    desc = "".join(format_exception(type(error), error, error.__traceback__))
    embed = make_timestamped_embed(color, title, escape_markdown(desc))

    value = f"{type(error).__name__}: {error}"
    embed.add_field(name="Simple Summary", value=escape_markdown(value))

    return embed
