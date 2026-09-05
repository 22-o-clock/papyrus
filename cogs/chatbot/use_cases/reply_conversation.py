from dataclasses import asdict, dataclass
from logging import getLogger
from pathlib import Path
from typing import Any, cast

import discord
from agents import Agent, ModelSettings, RunConfig, Runner, RunResult
from openai import AsyncOpenAI, BadRequestError, NotFoundError
from openai.types.shared import Reasoning

from cogs.chatbot.models.custom_profile import CustomProfile
from cogs.chatbot.models.reply_conversation import ConversationMessage, ReplyConversation
from cogs.chatbot.observability import observe_chatbot_api_call
from cogs.chatbot.repositories.reply_conversation import ReplyConversationRepository
from cogs.chatbot.services.conversation_agent import COMPACT_THRESHOLD, ObservedResponsesModel, build_agent_tools
from cogs.chatbot.services.conversation_tools import ConversationTools
from cogs.chatbot.services.message_delivery import reply_with_split_response

logger = getLogger(__name__)
PROMPT_DIRECTORY = Path(__file__).resolve().parents[1] / "prompt"


@dataclass
class GeneratedReply:
    """Discordへ送信する本文と、次回へ引き継ぐResponse ID。"""

    id: str | None
    output_text: str


def is_missing_response(error: BadRequestError | NotFoundError) -> bool:
    """継続先のResponseを参照できないAPIエラーかどうかを返します。"""
    return error.param == "previous_response_id" or (isinstance(error, NotFoundError) and "response" in str(error).lower())


class ReplyConversationUseCases:
    """呼び出しごとに返信の祖先だけをたどり、独立して生成・永続化します。"""

    def __init__(self, client: AsyncOpenAI, repository: ReplyConversationRepository) -> None:
        """回答生成に使うAPIクライアントと会話の保存先を設定します。"""
        self.client = client
        self.repository = repository

    async def load(
        self, message: discord.Message, tools: ConversationTools
    ) -> tuple[ReplyConversation, list[ConversationMessage]]:
        """送信時点の投稿と直近の継続点から、返信経路を復元します。

        Args:
            message: 今回の呼び出し元。
            tools: 同じサーバーの投稿を取得する処理。

        Returns:
            復元した会話と、継続点以降に追加する投稿。

        """
        pending = []
        current = await self.repository.get_message(message.id) or await tools.snapshot(message)
        seen: set[int] = set()
        state = ReplyConversation()
        while current.message_id not in seen:
            seen.add(current.message_id)
            # ユーザー投稿に兄弟枝の回答を結び付けない。完了したBot投稿だけが継続点。
            prior = await self.repository.get_turn(current.message_id) if current.is_assistant else None
            if prior is not None:
                state = prior
                break
            current = await self.repository.save_message(current)
            pending.append(current)
            if current.parent_id is None:
                break
            saved = await self.repository.get_message(current.parent_id)
            if saved is not None:
                current = saved
                continue
            try:
                channel = await tools.channel(current.parent_channel_id or current.channel_id)
                parent = await channel.fetch_message(current.parent_id)
                current = await tools.snapshot(parent)
            except (discord.HTTPException, ValueError, TypeError):
                state.missing_history = True
                break
        else:
            state.missing_history = True
        pending.reverse()
        state.messages.extend(pending)
        return state, pending

    async def respond(
        self, message: discord.Message, tools: ConversationTools, profile: CustomProfile | None
    ) -> tuple[list[discord.Message], str | None]:
        """生成中だけ入力中表示を出し、返信送信と次回の継続情報の保存を行います。

        Args:
            message: 呼び出し元のDiscord投稿。
            tools: 今回のサーバーに限定した取得処理。
            profile: 明示指定されたプロファイル。未指定なら会話から継承します。

        Returns:
            送信した投稿と、適用したプロファイル名。

        """
        async with message.channel.typing():
            state, response = await self._prepare_response(message, tools, profile)
        return await self._deliver(message, tools, state, response)

    async def _prepare_response(
        self, message: discord.Message, tools: ConversationTools, profile: CustomProfile | None
    ) -> tuple[ReplyConversation, GeneratedReply]:
        """返信経路とプロファイルを復元し、Agents SDKで回答を生成します。

        Args:
            message: 今回の呼び出し元の投稿。
            tools: サーバーの情報取得と投稿の変換に使う処理。
            profile: 明示指定されたプロファイル。未指定なら保存済みの設定を継承します。

        Returns:
            生成後の会話状態と、送信する本文を持つResponse。

        Raises:
            ValueError: モデルが回答本文を返さなかった場合。

        """
        state, pending = await self.load(message, tools)
        if profile is not None:
            state.profile = {key: value for key, value in asdict(profile).items() if key in {"name", "instructions", "model"}}
            state.messages[-1].content = profile.request_content
        state.model = (state.profile or {}).get("model", "system_default")
        if state.model == "system_default":
            state.model = "gpt-5.6-luna"
        instructions = (
            PROMPT_DIRECTORY.joinpath("reply_conversation.md")
            .read_text(encoding="utf-8")
            .format(
                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                channel_id=message.channel.id,
                function_name_short_term_memory="get_messages",
                function_name_channel_list="list_channels",
                function_name_attachment="get_attachment",
                function_name_user_list="list_members",
                function_name_long_term_memory="get_memory",
            )
        )
        instructions += f"\nあなたの名前は{tools.bot_name}です。"
        if state.profile:
            instructions += "\n基本指示と矛盾しない範囲で次のプロファイルを適用してください。\n" + state.profile["instructions"]

        model = ObservedResponsesModel(state, self.client)
        agent = Agent(
            name=tools.bot_name,
            instructions=instructions,
            model=model,
            tools=build_agent_tools(tools, state, message.id),
            model_settings=ModelSettings(
                reasoning=Reasoning(effort="medium"),
                parallel_tool_calls=False,
                context_management=[{"type": "compaction", "compact_threshold": COMPACT_THRESHOLD}],
            ),
        )
        inputs = (
            [m.as_input(include_media=True) for m in pending] if state.response_id else state.inputs(message.id, compact=False)
        )
        try:
            result = await self._run(agent, inputs, state.response_id)
        except (BadRequestError, NotFoundError) as exc:
            if model.completed_responses or not state.response_id or not is_missing_response(exc):
                raise
            inputs = await self.rebuild(state, message.id, tools)
            result = await self._run(agent, inputs, None)
        response = GeneratedReply(result.last_response_id, result.final_output)
        if not response.output_text:
            error_message = "モデルから回答本文が返りませんでした。"
            raise ValueError(error_message)
        return state, response

    async def _deliver(
        self, message: discord.Message, tools: ConversationTools, state: ReplyConversation, response: GeneratedReply
    ) -> tuple[list[discord.Message], str | None]:
        """回答を分割送信し、各返信から再開できる会話を保存します。

        Args:
            message: 返信先の投稿。
            tools: 送信済み投稿を保存用の情報へ変換する処理。
            state: 送信した回答を追記する会話状態。
            response: 送信する回答本文と継続用IDを持つResponse。

        Returns:
            送信した投稿とプロファイル名。最後の投稿だけにResponse IDを保存します。

        """
        # Discordに出した本文だけを復元履歴に残す。思考やツール結果を再構成時に再挿入しない。
        sent = await reply_with_split_response(message, response.output_text)
        for reply in sent:
            snapshot = await tools.snapshot(reply)
            await self.repository.save_message(snapshot)
            state.messages.append(snapshot)
            if reply.id != sent[-1].id:
                state.response_id = None
                await self.repository.save_turn(reply.id, state)
        state.response_id = response.id
        if sent:
            await self.repository.save_turn(sent[-1].id, state)
        return sent, (state.profile or {}).get("name")

    async def _run(self, agent: Agent, inputs: list[dict[str, Any]], previous_id: str | None) -> RunResult:
        """SDKに関数実行と継続を委ね、独自の生成回数上限は設けません。"""
        return await Runner.run(
            agent,
            input=cast("Any", inputs),
            previous_response_id=previous_id,
            auto_previous_response_id=True,
            max_turns=None,
            run_config=RunConfig(tracing_disabled=True),
        )

    async def rebuild(self, state: ReplyConversation, current_id: int, tools: ConversationTools) -> list[dict[str, Any]]:
        """参照不能な継続点を、保存済み本文と添付概要から復元します。

        Args:
            state: 継続IDを解除し、添付概要を更新する会話状態。
            current_id: 添付本体を残す今回の呼び出し元ID。
            tools: 過去の添付の保存済み解析結果を取得する処理。

        Returns:
            継続IDなしで送信する入力。本文は保存した状態を維持します。

        """
        state.response_id = None
        state.media_omitted.update(m.message_id for m in state.messages if m.message_id != current_id)
        for message in state.messages:
            if message.message_id == current_id:
                continue
            saved = {a.id: a for a in await tools.messages.get_attachments([message.message_id])}
            for attachment in message.attachments:
                analysis = saved.get(attachment.attachment_id)
                if analysis is not None and analysis.analysis_status == "completed":
                    attachment.summary = analysis.summary or ""
                    attachment.important_text = analysis.important_text or ""
                if not attachment.summary and attachment.kind in {"image", "pdf"}:
                    await self._describe_attachment(message, attachment.attachment_id, state.model)
        inputs = state.inputs(current_id, compact=True)
        logger.info("Rebuilt reply conversation (message_id=%s, retained_messages=%s)", current_id, len(state.messages))
        return inputs

    async def _describe_attachment(self, message: ConversationMessage, attachment_id: int, model: str) -> None:
        """既存の解析がない添付を要約し、会話内の添付情報を更新します。

        Args:
            message: 対象の添付を含む会話内の投稿。
            attachment_id: 要約する画像またはPDFのID。
            model: 添付の要約に使用するモデル名。

        """
        attachment = next(a for a in message.attachments if a.attachment_id == attachment_id)
        part = (
            {"type": "input_image", "image_url": attachment.url}
            if attachment.kind == "image"
            else {"type": "input_file", "file_url": attachment.url}
        )
        try:
            result = await observe_chatbot_api_call(
                "attachment_summary",
                model,
                self.client.responses.create(
                    model=model,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "添付を100文字程度で要約し、会話の理解に重要な文字情報も短く抜粋してください。",
                                },
                                cast("Any", part),
                            ],
                        }
                    ],
                ),
            )
            attachment.summary = result.output_text or "要約を取得できませんでした。"
        except (BadRequestError, NotFoundError):
            attachment.summary = "元資料を取得できず要約できませんでした。必要なら添付取得ツールで再取得してください。"
