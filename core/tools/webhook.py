from discord import TextChannel, Thread, Webhook


async def fetch_webhook(channel: TextChannel | Thread, *, name: str | None = None) -> Webhook:
    """メッセージ送信に使えるトークン付きWebhookを取得または作成する。

    Discord APIのチャンネルWebhook一覧にはWebhookトークンが含まれないため、
    既存Webhookをそのまま返すと ``Webhook.send`` が失敗する。名前を指定した
    場合は同名の既存Webhookをボット認証で削除して再作成する。
    """
    if isinstance(channel, Thread):
        if not isinstance(channel.parent, TextChannel):
            error_message = f"thread's parent should be TextChannel, actually got {type(channel)}"
            raise TypeError(error_message)

        channel = channel.parent

    if hooks := await channel.webhooks():
        if name is None:
            return hooks[0]

        for hook in hooks:
            if hook.name == name:
                await hook.delete(reason="Webhookトークンを再取得するため")
                break

    webhook_name = "bot-webhook-" + channel.name if name is None else name
    return await channel.create_webhook(name=webhook_name)
