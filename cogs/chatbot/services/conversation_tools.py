import datetime
import json
from collections.abc import Mapping
from typing import Any

import discord
from discord.http import Route

from cogs.chatbot.models.reply_conversation import ConversationAttachment, ConversationMessage
from cogs.chatbot.repositories.member_alias import ChatbotMemberAliasRepository
from cogs.chatbot.repositories.memory_document import ChatbotMemoryDocumentRepository
from cogs.chatbot.repositories.short_term_message import ChatbotShortTermMessageRepository

MESSAGE_LIMIT = 50
SEARCH_QUERY_LIMIT = 1024
SEARCH_INDEX_PENDING = 110000
SEARCH_LIMIT = 25
SEARCH_MAX_OFFSET = 9975


def function(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """全プロパティを必須にしたstrict形式の関数ツール定義を返します。"""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


ID = {"type": "string", "description": "Discord ID"}
FUNCTION_TOOLS = [
    function(
        "search_messages",
        "サーバー内の投稿をDiscordで検索します。保存期間の制限はありません。条件は組み合わせ可能です。",
        {
            "query": {
                "type": ["string", "null"],
                "maxLength": SEARCH_QUERY_LIMIT,
                "description": "本文の検索語。nullで指定なし。",
            },
            "channel_id": {"type": ["string", "null"], "description": "対象チャンネル。nullでサーバー全体。"},
            "author_id": {"type": ["string", "null"], "description": "投稿者ID。nullで指定なし。"},
            "after": {"type": ["string", "null"], "description": "開始日時(タイムゾーン付きISO 8601)。nullで指定なし。"},
            "before": {"type": ["string", "null"], "description": "終了日時(タイムゾーン付きISO 8601)。nullで指定なし。"},
            "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": SEARCH_LIMIT, "description": "nullなら25件。"},
            "offset": {
                "type": ["integer", "null"],
                "minimum": 0,
                "maximum": SEARCH_MAX_OFFSET,
                "description": "追加取得位置。nullなら0。",
            },
        },
    ),
    function(
        "get_message",
        "指定した投稿を直接取得します。投稿リンクの末尾2要素がチャンネルID・メッセージIDです。",
        {"channel_id": ID, "message_id": ID},
    ),
    function(
        "get_message_context",
        "指定投稿と同じチャンネルの前後の投稿を時系列順に取得します。返信関係による絞り込みは行いません。",
        {
            "channel_id": ID,
            "message_id": ID,
            "before_limit": {
                "type": ["integer", "null"],
                "minimum": 0,
                "maximum": MESSAGE_LIMIT,
                "description": "前の件数。nullなら10。",
            },
            "after_limit": {
                "type": ["integer", "null"],
                "minimum": 0,
                "maximum": MESSAGE_LIMIT,
                "description": "後の件数。nullなら10。",
            },
        },
    ),
    function("list_members", "このサーバーのメンバー一覧と、各メンバーを一意に識別できる登録済みの別名を取得します。", {}),
    function(
        "get_memory",
        "指定人物、ボット自身、サーバー共有の長期記憶を取得します。",
        {
            "kind": {"type": "string", "enum": ["person", "bot", "shared"]},
            "user_id": {"type": ["string", "null"], "description": "personでは必須。それ以外はnull。"},
        },
    ),
    function("list_channels", "閲覧可能なカテゴリ→チャンネル→スレッドの階層を取得します。アーカイブ済みも含みます。", {}),
    function(
        "get_messages",
        "チャンネルまたはスレッドの直近メッセージと添付情報を取得します。",
        {
            "channel_id": ID,
            "limit": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": MESSAGE_LIMIT,
                "description": "nullなら20件。最大50件。",
            },
            "before": {"type": ["string", "null"], "description": "追加取得する場合、このメッセージIDより前。"},
        },
    ),
    function(
        "get_attachment",
        "閲覧可能な任意の投稿の画像・PDF本体を取得します。",
        {
            "channel_id": ID,
            "message_id": ID,
            "attachment_id": ID,
        },
    ),
]


class ConversationTools:
    """呼び出し元サーバーでBotが閲覧できる情報を取得します。"""

    def __init__(
        self,
        guild: discord.Guild,
        messages: ChatbotShortTermMessageRepository,
        memory: ChatbotMemoryDocumentRepository,
        bot_id: int,
        aliases: ChatbotMemberAliasRepository,
    ) -> None:
        """対象サーバー、投稿・記憶の保存先、応答するBotを設定します。"""
        self.guild = guild
        self.messages = messages
        self.memory = memory
        self.bot_id = bot_id
        self.aliases = aliases
        self.bot_name = guild.me.display_name if guild.me else "Papyrus"

    async def channel(self, channel_id: int) -> discord.TextChannel | discord.Thread | discord.VoiceChannel:
        """投稿取得先を解決し、Botの閲覧権限を確認します。

        Args:
            channel_id: 同じサーバーのチャンネルまたはスレッドID。

        Returns:
            Botが投稿履歴を閲覧できるチャンネルまたはスレッド。

        Raises:
            TypeError: 投稿の取得に対応しないチャンネルの場合。
            ValueError: 対象サーバーが異なるか、必要な閲覧権限がない場合。
            discord.HTTPException: Discordからチャンネルを取得できない場合。

        """
        channel = self.guild.get_channel_or_thread(channel_id)
        if channel is None:
            channel = await self.guild.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            error_message = "メッセージを取得できないチャンネルです。"
            raise TypeError(error_message)
        member = self.guild.me
        if member is None or channel.guild.id != self.guild.id:
            error_message = "このサーバーのチャンネルではありません。"
            raise ValueError(error_message)
        permissions = channel.permissions_for(member)
        if not permissions.view_channel or not permissions.read_message_history:
            error_message = "ボットに履歴の閲覧権限がありません。"
            raise ValueError(error_message)
        return channel

    async def snapshot(self, message: discord.Message) -> ConversationMessage:
        """現在の投稿内容と保存済みの添付解析を会話用の投稿情報にまとめます。

        Args:
            message: 本文・投稿者・返信先・添付を取り込むDiscord投稿。

        Returns:
            添付の要約と解析済みの埋め込み画像情報も含む投稿情報。

        """
        stored = {a.id: a for a in await self.messages.get_attachments([message.id])}
        attachments = []
        for attachment in message.attachments:
            kind = "image" if (attachment.content_type or "").startswith("image/") else "file"
            if attachment.content_type == "application/pdf" or attachment.filename.lower().endswith(".pdf"):
                kind = "pdf"
            analysis = stored.get(attachment.id)
            attachments.append(
                ConversationAttachment(
                    attachment_id=attachment.id,
                    filename=attachment.filename,
                    url=attachment.url,
                    kind=kind,
                    summary=analysis.summary or "" if analysis else "",
                    important_text=analysis.important_text or "" if analysis else "",
                )
            )
        # 既存の添付解析が保存したEmbed画像も再取得可能にする。
        attachments.extend(
            ConversationAttachment(
                attachment_id=a.id,
                filename=a.filename,
                url=a.url,
                kind=a.kind,
                summary=a.summary or "",
                important_text=a.important_text or "",
            )
            for a in stored.values()
            if a.id < 0
        )
        reference = message.reference
        return ConversationMessage(
            message_id=message.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            created_at=message.created_at.isoformat(),
            content=message.content,
            parent_id=reference.message_id if reference else None,
            parent_channel_id=reference.channel_id if reference else None,
            is_assistant=message.author.id == self.bot_id,
            attachments=attachments,
        )

    async def execute(self, name: str, arguments: str) -> str | list[dict[str, Any]]:
        """モデルが要求した情報取得ツールを実行します。

        Args:
            name: 実行する関数ツール名。
            arguments: ツール引数を格納したJSONオブジェクト文字列。

        Returns:
            JSON文字列、または画像・PDFを含むツール出力用コンテンツ。

        Raises:
            ValueError: JSON、ツール名、引数の値が不正な場合。
            TypeError: 引数の形式または取得先のチャンネル種別が不正な場合。
            KeyError: 必須の引数が欠けている場合。
            discord.HTTPException: Discord上の情報を取得できない場合。

        """
        args = json.loads(arguments)
        if not isinstance(args, dict):
            error_message = "ツール引数はオブジェクトで指定してください。"
            raise TypeError(error_message)
        if name == "list_members":
            return await self._members()
        handlers = {
            "get_memory": self._memory,
            "get_messages": self._messages,
            "get_attachment": self._attachment,
            "search_messages": self._search,
            "get_message": self._message,
            "get_message_context": self._context,
        }
        if name == "list_channels":
            return await self._channels()
        if name in handlers:
            return await handlers[name](args)
        error_message = "未定義のツールです。"
        raise ValueError(error_message)

    async def _members(self) -> str:
        """サーバーに所属するメンバーと、有効な別名をJSONで返します。

        Returns:
            各メンバーのID・名前・表示名・Bot判定と、aliasesに格納した別名一覧。
            別名は既存の名前解決と同じ正規化済み表記で、曖昧・無効なものは含めません。

        """
        aliases_by_user: dict[int, list[str]] = {}
        for alias, user_id in (await self.aliases.get_active_aliases()).items():
            aliases_by_user.setdefault(user_id, []).append(alias)
        members = [
            {
                "user_id": str(member.id),
                "name": member.name,
                "display_name": member.display_name,
                "bot": member.bot,
                "aliases": sorted(aliases_by_user.get(member.id, [])),
            }
            async for member in self.guild.fetch_members(limit=None)
        ]
        return json.dumps(members, ensure_ascii=False)

    async def _memory(self, args: dict[str, Any]) -> str | list[dict[str, Any]]:
        """対象種別を検証し、人物・Bot自身・共有の長期記憶をJSONで返します。

        Args:
            args: kindと、人物を指定する場合のuser_idを持つ引数。

        Returns:
            指定種別の記憶文書のキーと本文。人物はサーバーへの所属を確認します。

        """
        kind = args["kind"]
        user_id = int(args["user_id"]) if args.get("user_id") else None
        if kind not in {"person", "bot", "shared"} or (kind == "person" and user_id is None):
            error_message = "長期記憶の対象が不正です。"
            raise ValueError(error_message)
        if kind == "person" and user_id is not None:
            await self.guild.fetch_member(user_id)
        documents = await self.memory.get_for_users({user_id} if user_id is not None else set())
        return json.dumps(
            [{"key": d.document_key, "content": d.content} for d in documents if d.document_type == kind],
            ensure_ascii=False,
        )

    async def _messages(self, args: dict[str, Any]) -> str | list[dict[str, Any]]:
        """閲覧可能な投稿を、添付の概要と追加取得用の位置情報付きで返します。

        Args:
            args: channel_id、取得件数limit、取得範囲の終端beforeを持つ引数。

        Returns:
            時系列順の投稿と次回指定できるnext_beforeを含むJSON。添付URLは省きます。

        Raises:
            ValueError: 取得件数が1〜50件の整数でない場合。

        """
        channel = await self.channel(int(args["channel_id"]))
        limit = args.get("limit") if args.get("limit") is not None else 20
        if type(limit) is not int or not 1 <= limit <= MESSAGE_LIMIT:
            error_message = "取得件数は1〜50件です。"
            raise ValueError(error_message)
        before = discord.Object(id=int(args["before"])) if args.get("before") else None
        messages = [await self.snapshot(m) async for m in channel.history(limit=limit, before=before)]
        return json.dumps(
            {
                "messages": [m.model_dump(exclude={"attachments": {"__all__": {"url"}}}) for m in reversed(messages)],
                "next_before": str(messages[-1].message_id) if messages else None,
            },
            ensure_ascii=False,
        )

    async def _message_data(self, message: discord.Message) -> dict[str, Any]:
        """投稿本文・添付概要・参照リンクを返します。"""
        snapshot = await self.snapshot(message)
        data = snapshot.model_dump(exclude={"attachments": {"__all__": {"url"}}})
        data["url"] = message.jump_url
        return data

    async def _message(self, args: dict[str, Any]) -> str:
        """現在閲覧できる指定投稿を直接取得します。"""
        channel = await self.channel(int(args["channel_id"]))
        message = await channel.fetch_message(int(args["message_id"]))
        return json.dumps(await self._message_data(message), ensure_ascii=False)

    async def _context(self, args: dict[str, Any]) -> str:
        """起点とその前後を取得し、時系列順で返します。"""
        before_limit = integer_argument(args, "before_limit", 10, 0, MESSAGE_LIMIT)
        after_limit = integer_argument(args, "after_limit", 10, 0, MESSAGE_LIMIT)
        channel = await self.channel(int(args["channel_id"]))
        anchor = await channel.fetch_message(int(args["message_id"]))
        before = (
            [m async for m in channel.history(limit=before_limit, before=anchor, oldest_first=False)] if before_limit else []
        )
        after = [m async for m in channel.history(limit=after_limit, after=anchor, oldest_first=True)] if after_limit else []
        messages = [*reversed(before), anchor, *after]
        return json.dumps(
            {"anchor_message_id": str(anchor.id), "messages": [await self._message_data(m) for m in messages]},
            ensure_ascii=False,
        )

    async def _search(self, args: dict[str, Any]) -> str:
        """Discordの検索結果を現在の閲覧権限で確認して返します。

        インデックス準備中は検索結果なしと区別し、再試行までの秒数を返します。
        ページ末尾は返却件数ではなく検索APIの総件数から判断します。
        """
        limit = integer_argument(args, "limit", SEARCH_LIMIT, 1, SEARCH_LIMIT)
        offset = integer_argument(args, "offset", 0, 0, SEARCH_MAX_OFFSET)
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort_by": "timestamp", "sort_order": "desc"}
        params.update(search_filters(args))
        if args.get("channel_id"):
            channel = await self.channel(int(args["channel_id"]))
            params["channel_id"] = [str(channel.id)]
        # discord.pyには検索用公開メソッドがないため、既存の認証・レート制御を使う。
        data = await self.guild._state.http.request(  # noqa: SLF001
            Route("GET", "/guilds/{guild_id}/messages/search", guild_id=self.guild.id), params=params
        )
        if data.get("code") == SEARCH_INDEX_PENDING:
            return json.dumps({"status": "indexing", "retry_after": data.get("retry_after", 2)})
        results = []
        seen: set[int] = set()
        unavailable: set[str] = set()
        for group in data["messages"]:
            for item in group:
                message_id = int(item["id"])
                if item.get("hit") is False or message_id in seen:
                    continue
                seen.add(message_id)
                try:
                    channel = await self.channel(int(item["channel_id"]))
                    message = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden, ValueError, TypeError):
                    unavailable.add(str(message_id))
                    continue
                results.append(await self._message_data(message))
        next_offset = offset + limit
        has_more = next_offset < data["total_results"]
        return json.dumps(
            {
                "status": "completed",
                "messages": results,
                "total_results": data["total_results"],
                "next_offset": next_offset if has_more and next_offset <= SEARCH_MAX_OFFSET else None,
                "pagination_limited": has_more and next_offset > SEARCH_MAX_OFFSET,
                "indexing": data.get("doing_deep_historical_index", False),
                "unavailable_message_ids": sorted(unavailable),
            },
            ensure_ascii=False,
        )

    async def _attachment(self, args: dict[str, Any]) -> str | list[dict[str, Any]]:
        """指定投稿を再取得し、現在のURLから添付本体を参照する入力を返します。

        Args:
            args: channel_id、message_id、attachment_idを持つ引数。

        Returns:
            画像・PDFの入力コンテンツ。非対応形式はURLと説明を含むJSON。

        Raises:
            ValueError: 指定した添付が投稿に存在しない場合。

        """
        channel = await self.channel(int(args["channel_id"]))
        message = await channel.fetch_message(int(args["message_id"]))
        snapshot = await self.snapshot(message)
        attachment = next((a for a in snapshot.attachments if a.attachment_id == int(args["attachment_id"])), None)
        if attachment is None:
            error_message = "添付が削除されたか、指定した投稿に存在しません。"
            raise ValueError(error_message)
        if attachment.kind == "image":
            return [{"type": "input_image", "image_url": attachment.url}]
        if attachment.kind == "pdf":
            return [{"type": "input_file", "file_url": attachment.url}]
        return json.dumps(
            {"filename": attachment.filename, "url": attachment.url, "error": "直接解析できる添付は画像とPDFです。"},
            ensure_ascii=False,
        )

    async def _channels(self) -> str:
        """閲覧可能なカテゴリ・チャンネル・スレッドを階層化して返します。

        Returns:
            categories配下のchannels、その配下のthreadsと、アーカイブ取得が不完全なIDのJSON。
            カテゴリ未所属のチャンネルはcategory_idがnullのグループに含めます。

        Raises:
            ValueError: サーバー内のBot情報を取得できない場合。
            discord.HTTPException: チャンネルまたはアクティブスレッドの取得に失敗した場合。

        """
        member = self.guild.me
        if member is None:
            error_message = "ボットのサーバー情報を取得できません。"
            raise ValueError(error_message)
        channels = {c.id: c for c in await self.guild.fetch_channels() if c.permissions_for(member).view_channel}
        threads = {t.id: t for t in await self.guild.active_threads() if t.permissions_for(member).view_channel}
        unavailable = []
        for channel in channels.values():
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                try:
                    async for thread in channel.archived_threads(limit=None):
                        threads[thread.id] = thread
                    if isinstance(channel, discord.TextChannel):
                        manage = channel.permissions_for(member).manage_threads
                        async for thread in channel.archived_threads(limit=None, private=True, joined=not manage):
                            threads[thread.id] = thread
                except discord.HTTPException:
                    unavailable.append(str(channel.id))
        return json.dumps(
            {
                "categories": build_channel_tree(channels, threads),
                "incomplete_archive_channels": unavailable,
            },
            ensure_ascii=False,
        )


def search_filters(args: dict[str, Any]) -> dict[str, Any]:
    """検索条件を検証し、Discord APIのフィルターへ変換します。"""
    params: dict[str, Any] = {}
    query = args.get("query")
    if query is not None:
        if not isinstance(query, str) or len(query) > SEARCH_QUERY_LIMIT:
            error_message = "検索語は1024文字以内の文字列です。"
            raise ValueError(error_message)
        params["content"] = query
    if args.get("author_id"):
        params["author_id"] = [str(int(args["author_id"]))]
    for key, parameter in (("after", "min_id"), ("before", "max_id")):
        if args.get(key):
            value = datetime.datetime.fromisoformat(args[key])
            if value.utcoffset() is None:
                error_message = "検索日時にはタイムゾーンを指定してください。"
                raise ValueError(error_message)
            params[parameter] = str(discord.utils.time_snowflake(value))
    if "min_id" in params and "max_id" in params and int(params["min_id"]) >= int(params["max_id"]):
        error_message = "開始日時は終了日時より前にしてください。"
        raise ValueError(error_message)
    return params


def integer_argument(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    """省略可能な整数引数を範囲検証して返します。"""
    value = args.get(key)
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        error_message = f"{key}は{minimum}〜{maximum}の整数で指定してください。"
        raise ValueError(error_message)
    return value


def build_channel_tree(
    channels: Mapping[int, discord.abc.GuildChannel], threads: Mapping[int, discord.Thread]
) -> list[dict[str, Any]]:
    """取得済みの閲覧可能なチャンネルとスレッドを親子関係でまとめます。

    Args:
        channels: カテゴリを含む、閲覧可能なチャンネルのID別辞書。
        threads: アクティブ・アーカイブ済みのスレッドのID別辞書。

    Returns:
        カテゴリごとのチャンネルとスレッド。未取得の親カテゴリはIDだけを残し、
        名前をnullにします。親チャンネルが取得範囲外のスレッドは含めません。

    """
    categories: dict[int | None, dict[str, Any]] = {
        channel.id: {"category_id": str(channel.id), "name": channel.name, "channels": []}
        for channel in sorted(channels.values(), key=lambda c: (c.position, c.id))
        if isinstance(channel, discord.CategoryChannel)
    }
    threads_by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for thread in sorted(threads.values(), key=lambda t: t.id):
        threads_by_parent.setdefault(thread.parent_id, []).append(
            {"channel_id": str(thread.id), "name": thread.name, "archived": thread.archived}
        )
    for channel in sorted(channels.values(), key=lambda c: (c.position, c.id)):
        if isinstance(channel, discord.CategoryChannel):
            continue
        category = categories.setdefault(
            channel.category_id,
            {
                "category_id": str(channel.category_id) if channel.category_id is not None else None,
                "name": None,
                "channels": [],
            },
        )
        category["channels"].append(
            {
                "channel_id": str(channel.id),
                "name": channel.name,
                "type": str(channel.type),
                "threads": threads_by_parent.get(channel.id, []),
            }
        )
    return list(categories.values())
