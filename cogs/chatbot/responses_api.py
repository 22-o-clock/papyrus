import datetime
import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from logging import getLogger
from typing import Any, Literal, Self

import dateutil
import discord
import tiktoken
from discord import Message
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, model_validator

from .channel_roles import ChannelRole
from .models.custom_profile import CustomProfile
from .observability import log_chatbot_api_call
from .prompt import draft_generator_prompt, memory_extraction_prompt, memory_reconciliation_prompt

logger = getLogger(__name__)

DRAFT_GENERATOR_MODEL = "gpt-5.6-terra"
CUSTOM_PROFILE_DEFAULT_MODEL = "gpt-5.6"
MEMORY_EXTRACTION_MODEL = "gpt-5.6-terra"
LOCAL_TIMEZONE = dateutil.tz.gettz("Asia/Tokyo")


@dataclass
class AttachmentInMemory:
    """短期文脈で参照する添付ファイルの解析情報。"""

    attachment_id: int
    filename: str
    kind: str
    analysis_status: str
    summary: str | None = None
    important_text: str | None = None

    def to_dict(self) -> dict[str, object]:
        """プロンプトに渡す添付情報を辞書形式で返します。"""
        result: dict[str, object] = {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "kind": self.kind,
            "analysis_status": self.analysis_status,
        }
        if self.analysis_status == "completed":
            result["summary"] = self.summary or ""
            result["important_text"] = self.important_text or ""
        return result


@dataclass
class MessageInMemory:
    """短期記憶内に保存されるメッセージを表すデータクラス。

    Attributes:
        message_id: メッセージのID
        author_id: メッセージの送信者のDiscordユーザーID
        author_name: メッセージの送信者の表示名
        content: メッセージの内容
        reply_to_message_id: 返信先のDiscordメッセージID
        mentioned_user_ids: メンションされたDiscordユーザーIDの一覧
        timestamp: メッセージが作成された日時
        is_stale_context: 長時間前の参考情報としてのみ使うメッセージか
        image_url: メッセージに含まれる画像のURL (存在する場合)
        pdf_url: メッセージに含まれるPDFのURL (存在する場合)
        attachments: 添付の解析状態と、完了済みの場合は要約・重要テキスト

    """

    message_id: int
    author_id: int
    author_name: str
    content: str
    reply_to_message_id: int | None
    mentioned_user_ids: list[int]
    timestamp: datetime.datetime
    is_stale_context: bool = False
    image_url: str | None = None
    pdf_url: str | None = None
    attachments: list[AttachmentInMemory] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """プロンプト作成に用いる要素のみを辞書形式で出力します。

        Returns:
            人物と返信先をDiscord IDで識別できる辞書

        """
        return {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "reply_to_message_id": self.reply_to_message_id,
            "mentioned_user_ids": self.mentioned_user_ids,
            "timestamp": self.timestamp.astimezone(LOCAL_TIMEZONE).isoformat(),
            "is_stale_context": self.is_stale_context,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }


class ShortTermMemory:
    """短期記憶を管理するクラス。メッセージの履歴をトークン数の制限内で保持します。"""

    def __init__(self, model: str = "gpt-5-") -> None:
        """短期メモリを初期化します。

        Args:
            model: トークンカウント用のtiktokenのモデル名 (デフォルト: "gpt-5-")

        """
        self.memory: list[MessageInMemory] = []
        self.encoding = tiktoken.encoding_for_model(model)

    async def append(self, message: Message) -> None:
        """メッセージを短期記憶に追加します。

        Args:
            message: 追加するDiscordメッセージ

        """
        # 1. デフォルト値の設定

        message_id = message.id
        author_id = message.author.id
        author_name = message.author.display_name
        content = message.clean_content
        reply_to_message_id = None
        mentioned_user_ids = [user.id for user in message.mentions]
        timestamp = message.created_at
        image_url = None
        pdf_url = None

        # 2. reply_to に関する特殊処理

        if message.message_snapshots:  # メッセージが転送である場合
            content = f"{author_name}がメッセージを転送: 「{message.message_snapshots[0].content}」"

        if message.type == discord.MessageType.reply and message.reference and message.reference.message_id:
            reply_to_message_id = message.reference.message_id
        elif message.type == discord.MessageType.reply:
            logger.warning(
                "Message is a reply but referenced message not found (ref_id=%s, channel_id=%s, guild_id=%s)",
                message.reference.message_id if message.reference else None,
                message.channel.id,
                message.guild.id if message.guild else None,
            )

        # 3. 添付ファイルに関する特殊処理

        for attachment in message.attachments:
            if attachment.content_type in ("image/jpeg", "image/png"):
                # OpenAI supports PNG (.png), JPEG (.jpeg, .jpg), WEBP (.webp), and Non-animated GIF (.gif).
                # Files with uncommon extensions (e.g., .jfif) may cause errors.
                # see https://platform.openai.com/docs/guides/images-vision
                image_url = attachment.url

            if attachment.content_type == "application/pdf":
                pdf_url = attachment.url

        # 4. メモリへの追加

        self.memory.append(
            MessageInMemory(
                message_id=message_id,
                author_id=author_id,
                author_name=author_name,
                content=content,
                reply_to_message_id=reply_to_message_id,
                mentioned_user_ids=mentioned_user_ids,
                timestamp=timestamp,
                image_url=image_url,
                pdf_url=pdf_url,
            )
        )

        # 5. メモリ内のメッセージを日時順にソート
        self.memory.sort(key=lambda m: m.timestamp)

        logger.debug("Current messages in memory: %s", self.memory)

    def to_json(self, *, content_overrides: dict[int, str] | None = None) -> str:
        """短期記憶内のメッセージをプロンプトに用いるJSON形式の文字列に変換します。

        Returns:
            人物と返信先をDiscord IDで識別できるJSON表現

        """
        serialized_messages = [message.to_dict() for message in self.memory]
        for serialized_message in serialized_messages:
            message_id = serialized_message["message_id"]
            if content_overrides is not None and isinstance(message_id, int) and message_id in content_overrides:
                serialized_message["content"] = content_overrides[message_id]
        return json.dumps(serialized_messages, ensure_ascii=False, indent=2)

    def forget(self, maximum_token: int = 5000) -> None:
        """メモリ内のメッセージを古い順に削除して、トークン数を制限以下に保ちます。

        Args:
            maximum_token: 保持される最大トークン数 (デフォルト: 5000)

        """
        while self.memory:
            text = json.dumps(
                [m.to_dict() for m in self.memory],
                ensure_ascii=False,
            )
            token_count = len(self.encoding.encode(text))

            if token_count <= maximum_token:
                break

            self.memory.pop(0)

        logger.debug(
            "Current messages in memory: %s tokens",
            len(
                self.encoding.encode(
                    json.dumps(
                        [m.to_dict() for m in self.memory],
                        ensure_ascii=False,
                    )
                )
            ),
        )

        logger.debug("Current memory: %s", self.memory)

    def contains_message(self, message_id: int) -> bool:
        """指定されたDiscordメッセージが現在の短期記憶に含まれるか確認します。"""
        return any(message.message_id == message_id for message in self.memory)

    def get_author_id(self, message_id: int) -> int | None:
        """指定されたDiscordメッセージの発言者IDを取得します。"""
        for message in self.memory:
            if message.message_id == message_id:
                return message.author_id
        return None

    def get_message(self, message_id: int) -> MessageInMemory | None:
        """指定されたDiscordメッセージの短期記憶データを取得します。"""
        return next((message for message in self.memory if message.message_id == message_id), None)

    def remove(self, message_id: int) -> None:
        """指定されたDiscordメッセージを短期記憶から除去します。"""
        self.memory = [message for message in self.memory if message.message_id != message_id]

    def set_attachment_analysis(
        self,
        message_id: int,
        attachment: AttachmentInMemory,
    ) -> None:
        """添付の解析状態を、対応するメッセージの文脈情報へ反映します。"""
        message = self.get_message(message_id)
        if message is None:
            return
        message.attachments = [
            existing_attachment
            for existing_attachment in message.attachments
            if existing_attachment.attachment_id != attachment.attachment_id
        ]
        message.attachments.append(attachment)

    def can_target_message(self, message_id: int) -> bool:
        """返信またはリアクションの対象にできる現在の会話内メッセージか判定します。"""
        return any(message.message_id == message_id and not message.is_stale_context for message in self.memory)

    def reset_for_new_conversation(self) -> None:
        """直前の投稿だけを古い参考情報として残し、現在の会話文脈をリセットします。"""
        if not self.memory:
            return

        last_message = self.memory[-1]
        last_message.is_stale_context = True
        self.memory = [last_message]

    def restore(self, messages: list[MessageInMemory]) -> None:
        """DBから復元したメッセージを時系列順で短期記憶へ設定します。"""
        self.memory = sorted(messages, key=lambda message: message.timestamp)
        self.forget()


class ResponseAction(StrEnum):
    """LLMが選択できるDiscord上の応答方法。"""

    SILENCE = "silence"
    REACTION = "reaction"
    REPLY = "reply"
    MESSAGE = "message"


class ShadowReason(StrEnum):
    """シャドー候補の行動判断を評価するための定型理由。"""

    NATURAL_CONTRIBUTION = "natural_contribution"
    HELPFUL_UNANSWERED_QUESTION = "helpful_unanswered_question"
    AVOID_INTERRUPTING_HUMANS = "avoid_interrupting_humans"
    NO_HELPFUL_CONTRIBUTION = "no_helpful_contribution"
    IDENTITY_UNCERTAIN = "identity_uncertain"
    COOLDOWN = "cooldown"


class LLMMessage(BaseModel):
    """OpenAI APIによって生成されるメッセージのデータモデル。

    Attributes:
        content: メッセージの内容
        action: Discord上で実行する応答方法
        reply_to_message_id: 返信先のDiscordメッセージID。通常投稿の場合はNone
        reaction_emoji: リアクションに使用するUnicode絵文字

    """

    action: ResponseAction
    content: str = ""
    reply_to_message_id: int | None = None
    reaction_emoji: str | None = None
    shadow_reason: ShadowReason = ShadowReason.NATURAL_CONTRIBUTION

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        """選択した行動の実行に必要なフィールドが揃っていることを保証します。"""
        if self.action is ResponseAction.REPLY and self.reply_to_message_id is None:
            msg = "reply action requires reply_to_message_id"
            raise ValueError(msg)
        if self.action is ResponseAction.REACTION and (self.reply_to_message_id is None or self.reaction_emoji is None):
            msg = "reaction action requires reply_to_message_id and reaction_emoji"
            raise ValueError(msg)
        if self.action in (ResponseAction.REPLY, ResponseAction.MESSAGE) and not self.content.strip():
            msg = "text response action requires content"
            raise ValueError(msg)
        silence_reasons = {
            ShadowReason.AVOID_INTERRUPTING_HUMANS,
            ShadowReason.NO_HELPFUL_CONTRIBUTION,
            ShadowReason.IDENTITY_UNCERTAIN,
            ShadowReason.COOLDOWN,
        }
        if self.shadow_reason in silence_reasons and self.action is not ResponseAction.SILENCE:
            msg = "the selected shadow_reason requires silence"
            raise ValueError(msg)
        if self.shadow_reason is ShadowReason.HELPFUL_UNANSWERED_QUESTION and self.action is ResponseAction.REACTION:
            msg = "helpful_unanswered_question cannot use reaction"
            raise ValueError(msg)
        return self

    def to_json(self, bot_name: str) -> str:
        """メッセージをJSON形式の文字列に変換します。

        Args:
            bot_name: botの名前

        Returns:
            メッセージのJSON表現

        """
        return json.dumps(
            {
                "author_name": bot_name,
                "action": self.action.value,
                "content": self.content,
                "reply_to_message_id": self.reply_to_message_id,
                "reaction_emoji": self.reaction_emoji,
                "shadow_reason": self.shadow_reason.value,
            },
            ensure_ascii=False,
            indent=2,
        )


class LongTermMemoryCandidate(BaseModel):
    """会話から抽出した根拠付き長期記憶候補。"""

    target_user_id: int | None
    external_entity_name: str | None = None
    target_resolution: Literal["member", "external", "unresolved"]
    kind: Literal["profile", "ongoing", "temporary", "shared"]
    content: str
    evidence_message_ids: list[int]
    source_type: Literal["self_statement", "third_party", "inference"]
    is_sensitive: bool


class MemberAliasCandidate(BaseModel):
    """明示的な根拠から抽出したサーバーメンバーの別名候補。"""

    alias: str
    target_user_id: int
    evidence_message_ids: list[int]


class LongTermMemoryCorrectionCandidate(BaseModel):
    """新しい事実を伴わない明示的な否定候補。"""

    target_user_id: int | None
    external_entity_name: str | None = None
    statement: str
    evidence_message_ids: list[int]
    source_type: Literal["self_statement", "third_party", "inference"]


class LongTermMemoryExtraction(BaseModel):
    """一括抽出した長期記憶候補の集合。"""

    candidates: list[LongTermMemoryCandidate]
    aliases: list[MemberAliasCandidate] = Field(default_factory=list)
    corrections: list[LongTermMemoryCorrectionCandidate] = Field(default_factory=list)


class MemoryReconciliation(BaseModel):
    """新しい情報と既存記憶の関係判定。"""

    action: Literal["keep", "supersede", "invalidate", "conflict"]
    existing_memory_ids: list[uuid.UUID] = Field(default_factory=list)


class LongTermMemoryReconciler:
    """新しい情報が既存記憶を訂正・否定するか判定します。"""

    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    async def reconcile(
        self,
        new_information: dict[str, object],
        existing_memories: list[dict[str, object]],
        *,
        correction_only: bool,
    ) -> MemoryReconciliation:
        """明確な矛盾だけを構造化された関係として返します。"""
        if not existing_memories:
            return MemoryReconciliation(action="keep")
        log_chatbot_api_call("memory_reconciliation", MEMORY_EXTRACTION_MODEL)
        response = await self.client.responses.parse(
            model=MEMORY_EXTRACTION_MODEL,
            reasoning={"effort": "none"},
            instructions=memory_reconciliation_prompt.MEMORY_RECONCILIATION_INSTRUCTIONS,
            input=json.dumps(
                {
                    "new_information": new_information,
                    "existing_memories": existing_memories,
                    "correction_only": correction_only,
                },
                ensure_ascii=False,
            ),
            text_format=MemoryReconciliation,
        )
        return response.output_parsed or MemoryReconciliation(action="keep")


class LongTermMemoryExtractor:
    """複数のDiscord投稿から長期記憶候補を抽出します。"""

    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    async def extract(
        self,
        messages: list[MessageInMemory],
        member_references: list[dict[str, object]],
    ) -> LongTermMemoryExtraction:
        """投稿一覧から、根拠付きの長期記憶候補を返します。"""
        log_chatbot_api_call("memory_extraction", MEMORY_EXTRACTION_MODEL, item_count=len(messages))
        api_response = await self.client.responses.parse(
            model=MEMORY_EXTRACTION_MODEL,
            reasoning={"effort": "none"},
            instructions=memory_extraction_prompt.MEMORY_EXTRACTION_INSTRUCTIONS,
            input=json.dumps(
                {"messages": [message.to_dict() for message in messages], "members": member_references},
                ensure_ascii=False,
            ),
            text_format=LongTermMemoryExtraction,
        )
        if api_response.output_parsed is None:
            logger.warning("Failed to parse long-term memory extraction response")
            return LongTermMemoryExtraction(candidates=[])
        return api_response.output_parsed


class DraftGenerator:
    """回答のドラフト生成を担当するクラス。OpenAI APIを使用して回答のドラフトを生成します。"""

    def __init__(self, client: AsyncOpenAI, bot_name: str) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント
            bot_name: botの名前

        """
        self.client = client
        self.bot_name = bot_name

    async def draft(
        self,
        short_term_memory: ShortTermMemory,
        channel_role: ChannelRole,
        *,
        is_unanswered_question: bool,
        long_term_memory_context: str = "",
        custom_profile: CustomProfile | None = None,
    ) -> LLMMessage:
        """メッセージのドラフト回答を生成します。

        Args:
            short_term_memory: メッセージ履歴
            channel_role: 対象チャンネルでのChatbotの役割
            is_unanswered_question: 人間からの回答を待った宛先のない質問への回答か
            long_term_memory_context: 検索済みの長期記憶コンテキスト
            custom_profile: 明示的なoption指定がある場合だけ適用するプロファイル

        Returns:
            生成されたドラフト回答を含むLLMMessageオブジェクト

        """
        # 履歴内の画像とPDFは、入力サイズを制御する方針が決まるまで直接の返信元だけを対象とする。

        content_overrides = (
            {custom_profile.request_message_id: custom_profile.request_content} if custom_profile is not None else None
        )
        serialized_memory = short_term_memory.to_json(content_overrides=content_overrides)
        llm_input: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            serialized_memory
                            + (f"\n\n長期記憶:\n{long_term_memory_context}" if long_term_memory_context else "")
                        ),
                    }
                ],
            }
        ]

        request_message = (
            short_term_memory.get_message(custom_profile.request_message_id)
            if custom_profile is not None
            else short_term_memory.memory[-1]
        )
        if request_message is None:
            request_message = short_term_memory.memory[-1]

        if request_message.image_url:
            llm_input[0]["content"].append({"type": "input_image", "image_url": request_message.image_url})

        if request_message.pdf_url:
            llm_input[0]["content"].append({"type": "input_file", "file_url": request_message.pdf_url})

        instructions = draft_generator_prompt.DRAFT_INSTRUCTIONS.format(
            bot_name=self.bot_name,
            channel_role=channel_role.value,
            unanswered_question_instruction=(
                "- この回答は、人間からの回答を待った宛先のない質問へのものです。"
                "短く明確に答えられる場合だけ応答してください。"
                "詳しい調査や長い説明が必要ならsilenceを選んでください。"
                if is_unanswered_question
                else ""
            ),
        )
        model = DRAFT_GENERATOR_MODEL
        reasoning_effort = "medium"
        if custom_profile is not None:
            model = CUSTOM_PROFILE_DEFAULT_MODEL if custom_profile.model == "system_default" else custom_profile.model
            reasoning_effort = "low"
            instructions += (
                f"\n\nこのリクエストではカスタムプロファイル `{custom_profile.name}` が明示的に選択されています。"
                "\n以下を基本指示と矛盾しない範囲で追加適用してください。"
                f"\n\n{custom_profile.instructions}"
            )

        log_chatbot_api_call(
            "draft_generation",
            model,
            custom_profile=(custom_profile.name if custom_profile is not None else None),
        )
        api_response = await self.client.responses.parse(
            input=llm_input,  # type: ignore
            instructions=instructions,
            model=model,
            reasoning={"effort": reasoning_effort},
            tools=[
                {
                    "type": "web_search",
                    "user_location": {"type": "approximate", "country": "JP"},
                },
                {
                    "type": "code_interpreter",
                    "container": {"type": "auto"},
                },
            ],
            text_format=LLMMessage,
        )

        if api_response.output_parsed is None:
            logger.warning("Failed to parse LLM response into LLMMessage")
            return LLMMessage(action=ResponseAction.SILENCE, shadow_reason=ShadowReason.NO_HELPFUL_CONTRIBUTION)

        return api_response.output_parsed


class ResponsePipeline:
    """短期記憶から一段階で最終回答を生成するクラス。"""

    def __init__(self, client: AsyncOpenAI, bot_name: str) -> None:
        """クラスを初期化します。

        Args:
            client: OpenAIの非同期クライアント
            bot_name: botの名前

        """
        self.draft_generator = DraftGenerator(client, bot_name)
        self.short_term_memory = ShortTermMemory()
        self.bot_name = bot_name

    async def add_message_to_memory(self, message: Message) -> None:
        """Discordのメッセージを短期記憶に追加します。

        Args:
            message: 追加するDiscordメッセージ

        """
        await self.short_term_memory.append(message)
        self.short_term_memory.forget()

    async def generate_response(
        self,
        channel_role: ChannelRole,
        *,
        is_unanswered_question: bool,
        long_term_memory_context: str = "",
        custom_profile: CustomProfile | None = None,
    ) -> LLMMessage:
        """短期記憶から最終回答を生成します。

        Args:
            channel_role: 対象チャンネルでのChatbotの役割
            is_unanswered_question: 人間からの回答を待った宛先のない質問への回答か
            long_term_memory_context: 検索済みの長期記憶コンテキスト
            custom_profile: 明示的なoption指定がある場合だけ適用するプロファイル

        Returns:
            最終回答を含むLLMMessageオブジェクト

        """
        return await self.draft_generator.draft(
            self.short_term_memory,
            channel_role,
            is_unanswered_question=is_unanswered_question,
            long_term_memory_context=long_term_memory_context,
            custom_profile=custom_profile,
        )
