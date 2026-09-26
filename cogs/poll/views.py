"""秘密投票のボタン、入力フォーム、投票画面。"""

import re
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from logging import getLogger
from typing import Any, Protocol, Self, runtime_checkable

import discord
from discord.ext import commands

from cogs.poll.constants import (
    DEFAULT_DURATION,
    MAX_OPTION_LENGTH,
    MAX_OPTIONS,
    MAX_QUESTION_LENGTH,
    VOTE_PANEL_TIMEOUT_SECONDS,
)
from cogs.poll.models import PollRecord, PollStatus
from core.exception import BotError

logger = getLogger(__name__)

POLL_COG_NAME = "Poll"
GENERIC_ERROR_MESSAGE = "処理中にエラーが発生しました。しばらくしてから再度お試しください... 💦"


class PollAction(StrEnum):
    """告知メッセージのボタンで行う操作。"""

    VOTE = "vote"
    CLOSE = "close"
    REOPEN = "reopen"


BUTTON_APPEARANCES = {
    PollAction.VOTE: ("投票する", discord.ButtonStyle.primary),
    PollAction.CLOSE: ("締め切る(作成者のみ)", discord.ButtonStyle.secondary),
    PollAction.REOPEN: ("再開する(作成者のみ)", discord.ButtonStyle.secondary),
}


@runtime_checkable
class PollButtonHandler(Protocol):
    """告知メッセージのボタン操作を受け付けるCog。"""

    async def handle_poll_button(self, interaction: discord.Interaction, poll_id: int, action: PollAction) -> None:
        """ボタン操作を処理する。"""
        ...


class PollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"poll:(?P<action>vote|close|reopen):(?P<poll_id>\d+)",
):
    """再起動後も操作できる、告知メッセージのボタン。"""

    def __init__(self, poll_id: int, action: PollAction) -> None:
        """投票IDと操作をcustom_idへ埋め込んだボタンを作る。"""
        label, style = BUTTON_APPEARANCES[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"poll:{action.value}:{poll_id}"))
        self.poll_id = poll_id
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """押されたボタンのcustom_idから復元する。"""
        del interaction, item
        return cls(int(match["poll_id"]), PollAction(match["action"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        """ボタン操作を秘密投票Cogへ渡す。"""
        handler = interaction.client.get_cog(POLL_COG_NAME) if isinstance(interaction.client, commands.Bot) else None
        if not isinstance(handler, PollButtonHandler):
            await send_ephemeral(interaction, "秘密投票機能が現在利用できません... 💦")
            return
        await run_guarded(interaction, handler.handle_poll_button(interaction, self.poll_id, self.action))


def build_poll_view(poll: PollRecord) -> discord.ui.View | None:
    """告知メッセージのボタンを返す。

    締め切り後の再開ボタンは結果メッセージに付ける。結果メッセージを投稿できなかった場合に限り、
    作成者が再開できるよう告知メッセージに付ける。
    """
    match poll.status:
        case PollStatus.OPEN:
            return _build_view(poll.id, (PollAction.VOTE, PollAction.CLOSE))
        case PollStatus.CLOSED if poll.result_message_id is None:
            return _build_view(poll.id, (PollAction.REOPEN,))
        case _:
            return None


def build_result_view(poll: PollRecord) -> discord.ui.View | None:
    """結果メッセージのボタンを返す。再開できる間だけ再開ボタンを付ける。"""
    return _build_view(poll.id, (PollAction.REOPEN,)) if poll.status is PollStatus.CLOSED else None


def _build_view(poll_id: int, actions: Sequence[PollAction]) -> discord.ui.View:
    """指定した操作のボタンを並べる。"""
    view = discord.ui.View(timeout=None)
    for action in actions:
        view.add_item(PollButton(poll_id, action))
    return view


class CreatePollModal(discord.ui.Modal, title="秘密投票の作成"):
    """質問と選択肢を入力するフォーム。"""

    question: discord.ui.TextInput = discord.ui.TextInput(label="質問", max_length=MAX_QUESTION_LENGTH)
    options: discord.ui.TextInput = discord.ui.TextInput(
        label=f"選択肢(1行に1つ、最大{MAX_OPTIONS}個)",
        style=discord.TextStyle.paragraph,
        placeholder="賛成\n反対",
        max_length=(MAX_OPTION_LENGTH + 1) * MAX_OPTIONS,
    )

    def __init__(self, submit: Callable[[discord.Interaction, str, str], Awaitable[None]]) -> None:
        """入力完了時に呼び出す処理を受け取る。"""
        super().__init__()
        self._submit = submit

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """入力された質問と選択肢で投票を作成する。"""
        await run_guarded(interaction, self._submit(interaction, self.question.value, self.options.value))


class ReopenPollModal(discord.ui.Modal, title="投票の再開"):
    """再開後の投票期間を入力するフォーム。"""

    duration: discord.ui.TextInput = discord.ui.TextInput(
        label="投票期間(例: 24h、1d12h、09/30 21:00)",
        default=DEFAULT_DURATION,
        max_length=32,
    )

    def __init__(self, submit: Callable[[discord.Interaction, str], Awaitable[None]]) -> None:
        """入力完了時に呼び出す処理を受け取る。"""
        super().__init__()
        self._submit = submit

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """入力された投票期間で投票を再開する。"""
        await run_guarded(interaction, self._submit(interaction, self.duration.value))


class VotePanel(discord.ui.View):
    """投票者本人にだけ表示する投票画面。

    変更可モードでは選択と同時に投票し、取り消しボタンを表示する。
    変更不可モードでは選択後に確認ボタンを押したときだけ投票する。
    """

    def __init__(  # noqa: PLR0913 - モードごとの表示と操作を受け取るため。
        self,
        options: Sequence[str],
        *,
        allow_multiple: bool,
        allow_change: bool,
        current: Sequence[int] | None,
        submit: Callable[[discord.Interaction, list[int]], Awaitable[None]],
        withdraw: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        """選択肢と、投票・取り消し時に呼び出す処理を受け取る。"""
        super().__init__(timeout=VOTE_PANEL_TIMEOUT_SECONDS)
        self._options = tuple(options)
        self._allow_change = allow_change
        self._submit = submit
        self._withdraw = withdraw
        self._pending: list[int] = []
        selected = set(current or ())
        self.choice_select.max_values = len(self._options) if allow_multiple else 1
        self.choice_select.options = [
            discord.SelectOption(label=option, value=str(index), default=index in selected)
            for index, option in enumerate(self._options)
        ]
        if allow_change:
            self.action_button.label = "投票を取り消す"
            self.action_button.style = discord.ButtonStyle.danger
            self.action_button.disabled = not selected
        else:
            self.action_button.label = "この内容で投票する(変更不可)"
            self.action_button.style = discord.ButtonStyle.primary
            self.action_button.disabled = True

    @discord.ui.select(placeholder="選択肢を選んでください", min_values=1)
    async def choice_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        """変更可モードではすぐに投票し、変更不可モードでは確認ボタンを有効にする。"""
        choices = sorted(int(value) for value in select.values)
        if self._allow_change:
            await run_guarded(interaction, self._submit(interaction, choices))
            return
        self._pending = choices
        self.action_button.disabled = False
        for option in select.options:
            option.default = int(option.value) in choices
        selected = "、".join(discord.utils.escape_markdown(self._options[index]) for index in choices)
        await interaction.response.edit_message(
            content=(
                f"選択中: **{selected}**\n「この内容で投票する」を押すと投票します。**投票後の変更・取り消しはできません。**"
            ),
            view=self,
        )

    @discord.ui.button()
    async def action_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """変更可モードでは投票を取り消し、変更不可モードでは選択中の内容を投票する。"""
        del button
        if self._allow_change:
            await run_guarded(interaction, self._withdraw(interaction))
        else:
            await run_guarded(interaction, self._submit(interaction, self._pending))


async def run_guarded(interaction: discord.Interaction, operation: Awaitable[None]) -> None:
    """ボタンやフォームの処理を実行し、失敗を操作者にだけ伝える。

    discord.pyの既定のエラーログは操作中の部品の状態を出力し、投票画面では選択内容を含み得るため、
    例外はここで処理して、ログには例外の種類と発生箇所だけを残す。
    """
    try:
        await operation
    except BotError as error:
        await send_ephemeral(interaction, error.message)
    except Exception:
        logger.exception("Failed to handle a poll interaction")
        try:
            await send_ephemeral(interaction, GENERIC_ERROR_MESSAGE)
        except discord.HTTPException:
            logger.exception("Failed to send a poll error message")


async def send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """操作者にだけ見えるメッセージを、メンション通知なしで送る。"""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
