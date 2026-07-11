import unittest

from cogs.chatbot.channel_roles import ChannelRole
from cogs.chatbot.chatbot_cog import should_respond


class ShouldRespondTest(unittest.TestCase):
    def test_assistant_responds_to_mention(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=True,
            replied_to_bot=False,
            spontaneous_chat_reply=False,
        )

        if not result:
            self.fail("assistantがメンションへ応答しません")

    def test_assistant_responds_to_reply(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=True,
            spontaneous_chat_reply=False,
        )

        if not result:
            self.fail("assistantがボットへの返信へ応答しません")

    def test_assistant_ignores_spontaneous_reply_decision(self) -> None:
        result = should_respond(
            ChannelRole.ASSISTANT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=True,
        )

        if result:
            self.fail("assistantが明示的に呼ばれていない投稿へ応答します")

    def test_chat_can_respond_spontaneously(self) -> None:
        result = should_respond(
            ChannelRole.CHAT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=True,
        )

        if not result:
            self.fail("chatが自発返信の判定を反映しません")

    def test_chat_ignores_when_not_called_and_spontaneous_decision_is_false(self) -> None:
        result = should_respond(
            ChannelRole.CHAT,
            mentioned_bot=False,
            replied_to_bot=False,
            spontaneous_chat_reply=False,
        )

        if result:
            self.fail("chatが返信不要の判定でも応答します")
