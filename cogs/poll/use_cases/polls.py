"""秘密投票の作成、投票、締め切り、再開、確定を扱うユースケース。"""

import datetime
import io
from functools import partial
from logging import getLogger

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import format_dt

from cogs.poll.constants import JST
from cogs.poll.models import BallotOutcome, PollRecord, PollStatus
from cogs.poll.repositories.poll import PollRepository
from cogs.poll.services.ballot_key import make_ballot_key
from cogs.poll.services.duration import parse_deadline
from cogs.poll.services.presentation import (
    build_electorate_embed,
    build_poll_embed,
    build_reopen_embed,
    build_result_embed,
    describe_choices,
    describe_eligibility,
)
from cogs.poll.services.validation import parse_options, parse_question
from cogs.poll.views import (
    CreatePollModal,
    PollAction,
    ReopenPollModal,
    VotePanel,
    build_poll_view,
    build_result_view,
    send_ephemeral,
)
from core.exception import ArgumentError

logger = getLogger(__name__)

NOT_OPEN_MESSAGE = "この投票は受付中ではありません。"
AUTOCOMPLETE_NAME_LIMIT = 100


class PollUseCases:
    """Discordの操作と、投票の保存・集計を接続する。

    ログには投票IDだけを出力し、操作したユーザーと選択内容を同時に残さない。
    """

    def __init__(
        self,
        bot: commands.Bot,
        repository: PollRepository,
        *,
        ballot_secret: bytes,
        electorate_role_id: int,
    ) -> None:
        """Botの接続、保存先、投票者の秘匿に使う秘密鍵、既定の対象ロールを受け取る。"""
        self._bot = bot
        self._repository = repository
        self._ballot_secret = ballot_secret
        self._electorate_role_id = electorate_role_id

    async def start_create(
        self,
        interaction: discord.Interaction,
        *,
        duration: str,
        allow_multiple: bool,
        allow_change: bool,
        role: discord.Role | None,
    ) -> None:
        """設定を検証してから、質問と選択肢の入力フォームを表示する。"""
        guild = _require_guild(interaction)
        target_role, electorate_role = self._resolve_roles(guild, role)
        parse_deadline(duration, _now())
        submit = partial(
            self._create,
            duration=duration,
            allow_multiple=allow_multiple,
            allow_change=allow_change,
            role=target_role,
            electorate_role=electorate_role,
        )
        await interaction.response.send_modal(CreatePollModal(submit))

    async def handle_button(self, interaction: discord.Interaction, poll_id: int, action: PollAction) -> None:
        """告知メッセージのボタン操作を処理する。"""
        poll = await self._repository.get(poll_id)
        if poll is None:
            await send_ephemeral(interaction, "この投票は見つかりません。")
            return
        match action:
            case PollAction.VOTE:
                await self._open_vote_panel(interaction, poll)
            case PollAction.CLOSE:
                await self._close_by_creator(interaction, poll)
            case PollAction.REOPEN:
                await self._start_reopen(interaction, poll)

    async def show_electorate(self, interaction: discord.Interaction, poll_id_text: str) -> None:
        """作成時点の有権者リストを、実行者にだけ表示する。"""
        guild = _require_guild(interaction)
        poll = await self._get_guild_poll(guild, poll_id_text)
        user_ids = await self._repository.list_electorate(poll.id)
        embed, needs_attachment = build_electorate_embed(poll, user_ids, self._electorate_role_id)
        files: list[discord.File] = []
        if needs_attachment:
            lines = [f"{_member_name(guild, user_id)} ({user_id})" for user_id in user_ids]
            files.append(discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"poll_{poll.id}_electorate.txt"))
        await interaction.response.send_message(
            embed=embed, files=files, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def autocomplete_polls(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """サーバー内の投票を、新しい順に候補として返す。"""
        if interaction.guild_id is None:
            return []
        polls = await self._repository.search(interaction.guild_id, current)
        return [
            app_commands.Choice(name=_truncate(f"#{poll_id} {question}", AUTOCOMPLETE_NAME_LIMIT), value=str(poll_id))
            for poll_id, question in polls
        ]

    async def process_due_polls(self) -> None:
        """締め切り時刻や確定時刻を過ぎた投票を処理し、結果を投稿する。"""
        now = _now()
        for poll_id in await self._repository.list_due_to_close(now):
            try:
                if (closed := await self._repository.close(poll_id, now)) is not None:
                    await self._after_close(closed)
            except Exception:
                logger.exception("Failed to close a poll (poll_id=%s)", poll_id)
        for poll_id in await self._repository.list_due_to_finalize(now):
            try:
                if (finalized := await self._repository.finalize(poll_id, now)) is not None:
                    await self._after_finalize(finalized)
            except Exception:
                logger.exception("Failed to finalize a poll (poll_id=%s)", poll_id)

    async def refresh_announcement(self, poll_id: int) -> None:
        """告知メッセージを、投票の現在の状態へ更新する。"""
        poll = await self._repository.get(poll_id)
        if poll is None or poll.message_id is None:
            return
        voter_count = await self._repository.count_voters(poll_id) if poll.status is PollStatus.OPEN else 0
        electorate_count = await self._repository.count_electorate(poll_id)
        message = self._bot.get_partial_messageable(poll.channel_id).get_partial_message(poll.message_id)
        try:
            await message.edit(
                embed=build_poll_embed(poll, voter_count, electorate_count, self._electorate_role_id),
                view=build_poll_view(poll),
            )
        except discord.HTTPException:
            logger.exception("Failed to update a poll announcement (poll_id=%s)", poll_id)

    async def _after_close(self, poll: PollRecord) -> None:
        """締め切り時点の結果を新しいメッセージとして投稿し、告知メッセージを締め切り済みにする。

        結果や状態の変化は、会話の流れに埋もれないよう、告知メッセージの編集ではなく新しいメッセージで知らせる。
        """
        await self._post_result(poll)
        await self.refresh_announcement(poll.id)

    async def _after_reopen(self, poll: PollRecord) -> None:
        """再開を新しいメッセージで知らせ、前回の結果メッセージを前回の結果として扱う。"""
        await self._edit_result_message(
            poll, embed=build_result_embed(poll, await self._repository.count_electorate(poll.id), self._electorate_role_id)
        )
        await self._post_reply(poll, build_reopen_embed(poll))
        await self.refresh_announcement(poll.id)

    async def _after_finalize(self, poll: PollRecord) -> None:
        """確定した結果を新しいメッセージとして投稿し、前回の結果メッセージから再開ボタンを外す。"""
        await self._edit_result_message(poll)
        await self._post_result(poll)
        await self.refresh_announcement(poll.id)

    async def _post_result(self, poll: PollRecord) -> None:
        """結果メッセージを投稿し、告知メッセージからのリンク先として記録する。"""
        electorate_count = await self._repository.count_electorate(poll.id)
        message = await self._post_reply(
            poll, build_result_embed(poll, electorate_count, self._electorate_role_id), build_result_view(poll)
        )
        if message is not None:
            await self._repository.attach_result_message(poll.id, message.id)

    async def _post_reply(
        self, poll: PollRecord, embed: discord.Embed, view: discord.ui.View | None = None
    ) -> discord.Message | None:
        """告知メッセージへの返信として、メンション通知なしで新しいメッセージを投稿する。"""
        reference = (
            discord.MessageReference(message_id=poll.message_id, channel_id=poll.channel_id, fail_if_not_exists=False)
            if poll.message_id is not None
            else discord.utils.MISSING
        )
        try:
            return await self._bot.get_partial_messageable(poll.channel_id).send(
                embed=embed,
                view=view or discord.utils.MISSING,
                reference=reference,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Failed to post a poll message (poll_id=%s)", poll.id)
            return None

    async def _edit_result_message(self, poll: PollRecord, *, embed: discord.Embed | None = None) -> None:
        """直近の結果メッセージの再開ボタンを外し、必要に応じて表示を更新する。"""
        if poll.result_message_id is None:
            return
        message = self._bot.get_partial_messageable(poll.channel_id).get_partial_message(poll.result_message_id)
        try:
            if embed is None:
                await message.edit(view=None)
            else:
                await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            logger.exception("Failed to update a poll result message (poll_id=%s)", poll.id)

    def _resolve_roles(self, guild: discord.Guild, role: discord.Role | None) -> tuple[discord.Role, discord.Role]:
        """対象ロールと有権者ロールを返す。対象ロールの省略時は有権者ロールとし、@everyoneは認めない。

        有権者ロールは、対象ロールを指定した場合も投票に必須とする。
        """
        electorate_role = guild.get_role(self._electorate_role_id)
        if electorate_role is None:
            message = "有権者ロールが見つかりません。Bot管理者に設定を確認してもらってください... 💦"
            raise ArgumentError(message)
        target = role or electorate_role
        if target.is_default():
            message = "@everyone は対象ロールに指定できません... 💦"
            raise ArgumentError(message)
        return target, electorate_role

    async def _create(  # noqa: PLR0913 - フォームの入力とコマンドの設定を合わせて受け取るため。
        self,
        interaction: discord.Interaction,
        question_text: str,
        options_text: str,
        *,
        duration: str,
        allow_multiple: bool,
        allow_change: bool,
        role: discord.Role,
        electorate_role: discord.Role,
    ) -> None:
        """作成時点の有権者リストとともに投票を保存し、告知メッセージを投稿する。"""
        guild = _require_guild(interaction)
        question = parse_question(question_text)
        options = parse_options(options_text)
        closes_at = parse_deadline(duration, _now())
        if interaction.channel_id is None:
            message = "チャンネル情報を取得できませんでした... 💦"
            raise ArgumentError(message)
        if not guild.chunked:
            await guild.chunk()
        electorate_ids = sorted(
            {member.id for member in role.members if not member.bot and member.get_role(electorate_role.id) is not None}
        )
        if not electorate_ids:
            if role == electorate_role:
                message = f"{role.mention} を持つメンバーがいないため、投票を作成できません... 💦"
            else:
                message = (
                    f"{role.mention} と {electorate_role.mention} の両方を持つメンバーがいないため、投票を作成できません... 💦"
                )
            raise ArgumentError(message)

        # 入力の誤りは本人にだけ伝えられるよう、検証を終えてから、DBの応答を待つ間の応答期限を延ばす。
        await interaction.response.defer(thinking=True)
        poll = await self._repository.create(
            guild_id=guild.id,
            channel_id=interaction.channel_id,
            creator_id=interaction.user.id,
            question=question,
            options=options,
            allow_multiple=allow_multiple,
            allow_change=allow_change,
            target_role_id=role.id,
            closes_at=closes_at,
            electorate_ids=electorate_ids,
        )
        try:
            message = await interaction.followup.send(
                embed=build_poll_embed(poll, 0, len(electorate_ids), self._electorate_role_id),
                view=build_poll_view(poll) or discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
            )
        except Exception:
            await self._repository.delete(poll.id)
            raise
        await self._repository.attach_message(poll.id, message.id)

    async def _open_vote_panel(self, interaction: discord.Interaction, poll: PollRecord) -> None:
        """有権者にだけ、本人にしか見えない投票画面を表示する。"""
        if not poll.accepts_votes(_now()):
            await send_ephemeral(interaction, NOT_OPEN_MESSAGE)
            return
        if not await self._repository.is_eligible(poll.id, interaction.user.id):
            await send_ephemeral(interaction, self._not_eligible_message(poll))
            return
        voter_key = self._voter_key(poll.id, interaction.user.id)
        if poll.allow_change:
            current = await self._repository.get_choices(poll.id, voter_key)
            content = (
                f"{describe_choices(poll.options, current)}\n"
                "選択肢を選ぶと、すぐに投票されます。締め切りまでは何度でも変更・取り消しできます。"
            )
        else:
            if await self._repository.has_voted(poll.id, voter_key):
                await send_ephemeral(interaction, "投票済みです。この投票は変更不可モードのため、変更・取り消しはできません。")
                return
            current = None
            content = "選択肢を選び、「この内容で投票する」を押してください。**投票後の変更・取り消しはできません。**"
        await interaction.response.send_message(
            content, view=self._vote_panel(poll, current), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    def _vote_panel(self, poll: PollRecord, current: tuple[int, ...] | None) -> VotePanel:
        """投票画面を作る。"""
        return VotePanel(
            poll.options,
            allow_multiple=poll.allow_multiple,
            allow_change=poll.allow_change,
            current=current,
            submit=partial(self._submit_ballot, poll.id),
            withdraw=partial(self._withdraw_ballot, poll.id),
        )

    async def _submit_ballot(self, poll_id: int, interaction: discord.Interaction, choices: list[int]) -> None:
        """投票画面で選ばれた内容を投票する。"""
        poll = await self._repository.get(poll_id)
        if poll is None:
            await interaction.response.edit_message(content="この投票は見つかりません。", view=None)
            return
        _validate_choices(poll, choices)
        voter_key = self._voter_key(poll_id, interaction.user.id)
        result = await self._repository.cast_ballot(poll_id, interaction.user.id, voter_key, choices, _now())
        match result.outcome:
            case BallotOutcome.ACCEPTED if poll.allow_change:
                await interaction.response.edit_message(
                    content=(
                        f"投票を受け付けました。{describe_choices(poll.options, choices)}\n"
                        "締め切りまでは、選び直して変更したり、取り消したりできます。"
                    ),
                    view=self._vote_panel(poll, tuple(choices)),
                )
            case BallotOutcome.ACCEPTED:
                await interaction.response.edit_message(
                    content=(
                        f"投票を受け付けました。{describe_choices(poll.options, choices)}\n"
                        "変更不可モードのため、誰が何を選んだかは記録されず、後から確認・変更することはできません。"
                    ),
                    view=None,
                )
            case BallotOutcome.ALREADY_VOTED:
                await interaction.response.edit_message(
                    content="投票済みです。この投票は変更不可モードのため、変更・取り消しはできません。", view=None
                )
                return
            case BallotOutcome.NOT_ELIGIBLE:
                await interaction.response.edit_message(content=self._not_eligible_message(poll), view=None)
                return
            case _:
                await interaction.response.edit_message(content=NOT_OPEN_MESSAGE, view=None)
                return
        await self.refresh_announcement(poll_id)

    async def _withdraw_ballot(self, poll_id: int, interaction: discord.Interaction) -> None:
        """変更可モードの票を取り消す。"""
        poll = await self._repository.get(poll_id)
        if poll is None:
            await interaction.response.edit_message(content="この投票は見つかりません。", view=None)
            return
        result = await self._repository.withdraw_ballot(poll_id, self._voter_key(poll_id, interaction.user.id), _now())
        if result.outcome is not BallotOutcome.ACCEPTED:
            await interaction.response.edit_message(content=NOT_OPEN_MESSAGE, view=None)
            return
        await interaction.response.edit_message(
            content="投票を取り消しました。締め切りまでは、選び直して再度投票できます。",
            view=self._vote_panel(poll, None),
        )
        await self.refresh_announcement(poll_id)

    async def _close_by_creator(self, interaction: discord.Interaction, poll: PollRecord) -> None:
        """作成者の操作で、受付中の投票を締め切る。"""
        if interaction.user.id != poll.creator_id:
            await send_ephemeral(interaction, "投票を締め切れるのは作成者だけです。")
            return
        closed = await self._repository.close(poll.id, _now())
        if closed is None or closed.finalizes_at is None:
            await send_ephemeral(interaction, NOT_OPEN_MESSAGE)
            return
        await send_ephemeral(
            interaction, f"締め切り、結果を投稿しました。{format_dt(closed.finalizes_at, 'f')}までは再開できます。"
        )
        await self._after_close(closed)

    async def _start_reopen(self, interaction: discord.Interaction, poll: PollRecord) -> None:
        """作成者に、再開後の投票期間の入力フォームを表示する。"""
        if interaction.user.id != poll.creator_id:
            await send_ephemeral(interaction, "投票を再開できるのは作成者だけです。")
            return
        if not poll.can_reopen(_now()):
            await send_ephemeral(interaction, "再開できる期間を過ぎています。")
            return
        await interaction.response.send_modal(ReopenPollModal(partial(self._reopen, poll.id)))

    async def _reopen(self, poll_id: int, interaction: discord.Interaction, duration: str) -> None:
        """入力された投票期間で、票を引き継いで投票を再開する。"""
        poll = await self._repository.get(poll_id)
        if poll is None or interaction.user.id != poll.creator_id:
            await send_ephemeral(interaction, "投票を再開できるのは作成者だけです。")
            return
        now = _now()
        reopened = await self._repository.reopen(poll_id, now, parse_deadline(duration, now))
        if reopened is None:
            await send_ephemeral(interaction, "再開できる期間を過ぎています。")
            return
        await send_ephemeral(interaction, f"再開しました。{format_dt(reopened.closes_at, 'f')}に締め切ります。")
        await self._after_reopen(reopened)

    async def _get_guild_poll(self, guild: discord.Guild, poll_id_text: str) -> PollRecord:
        """実行したサーバーの投票を返す。

        Raises:
            ArgumentError: IDの形式が不正な場合、または投票が見つからない場合。

        """
        poll_id = int(poll_id_text.removeprefix("#")) if poll_id_text.removeprefix("#").isdecimal() else None
        poll = None if poll_id is None else await self._repository.get(poll_id)
        if poll is None or poll.guild_id != guild.id or poll.message_id is None:
            message = "指定した投票が見つかりません... 💦"
            raise ArgumentError(message)
        return poll

    def _not_eligible_message(self, poll: PollRecord) -> str:
        """有権者でない場合の案内を返す。"""
        return (
            f"この投票の有権者ではありません。{describe_eligibility(poll, self._electorate_role_id)}だけが投票できます。\n"
            "`/poll electorate` で有権者リストを確認できます。"
        )

    def _voter_key(self, poll_id: int, user_id: int) -> str:
        """投票者の秘匿キーを返す。"""
        return make_ballot_key(self._ballot_secret, poll_id, user_id)


def _validate_choices(poll: PollRecord, choices: list[int]) -> None:
    """投票画面から届いた選択が、投票の形式に合うかを検証する。"""
    max_choices = len(poll.options) if poll.allow_multiple else 1
    if (
        not 1 <= len(choices) <= max_choices
        or len(set(choices)) != len(choices)
        or any(not 0 <= index < len(poll.options) for index in choices)
    ):
        message = "選択内容が不正です... 💦"
        raise ArgumentError(message)


def _require_guild(interaction: discord.Interaction) -> discord.Guild:
    """サーバー内の操作であることを確認する。"""
    if interaction.guild is None:
        message = "サーバー内で実行してください... 💦"
        raise ArgumentError(message)
    return interaction.guild


def _member_name(guild: discord.Guild, user_id: int) -> str:
    """メンバーの表示名を返す。サーバーを抜けたメンバーは不明とする。"""
    member = guild.get_member(user_id)
    return member.display_name if member is not None else "(サーバーにいないメンバー)"


def _truncate(text: str, limit: int) -> str:
    """上限を超える文字列を省略記号付きで切り詰める。"""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _now() -> datetime.datetime:
    """JSTの現在時刻を返す。"""
    return datetime.datetime.now(tz=JST)
