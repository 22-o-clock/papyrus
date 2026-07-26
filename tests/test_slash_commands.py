from unittest import TestCase

from discord import app_commands

from cogs.api_usage.api_usage import ApiUsageReporter
from cogs.chatbot.chatbot import ChatBot
from cogs.cynicism.cynicism import Cynicism
from cogs.monitor.monitor import Monitor
from cogs.moving.moving import Moving
from cogs.remind.remind import Notify
from cogs.speak.speak import Speak
from cogs.talkdata.talkdata import TalkData
from cogs.voice.voice import ArknightsVoice
from cogs.voicevox.voicevox import Voicevox

EXPECTED_GROUP_COMMANDS = {
    "api_usage": {"report", "schedule", "status"},
    "arknights": {"doctor", "doctor_rest", "random", "search"},
    "chatbot": {
        "aliases_export",
        "aliases_import",
        "profile_disable",
        "profile_list",
        "profile_save",
        "profile_show",
        "role_reset",
        "role_set",
        "role_show",
    },
    "copy": {"all", "range", "to_new_thread"},
    "cynicism": {"pause", "publish", "ranking", "resume", "status", "weight"},
    "moderation": {
        "expression_config",
        "post_ban",
        "post_unban",
        "reaction_ban",
        "reaction_remove",
        "reaction_remove_bot",
        "reaction_unban",
    },
    "reminder": {"list", "remove"},
    "talkdata": {
        "channel_upsert",
        "member_upsert",
        "message_history",
        "messages_insert_all",
        "messages_insert_current",
    },
    "voice": {"connect", "disconnect", "pause", "speaker_set", "speaker_show"},
}

COMMAND_GROUPS = (
    ApiUsageReporter.api_usage,
    ArknightsVoice.arknights,
    ChatBot.chatbot,
    Moving.copy,
    Cynicism.cynicism,
    Monitor.moderation,
    Notify.reminder,
    TalkData.talkdata,
    Voicevox.voice,
)

INDEPENDENT_COMMANDS = (Notify.remind, Speak.choice_command, Speak.hi)
EXPECTED_SLASH_COMMAND_COUNT = 47


def ensure(condition: object) -> None:
    """条件を満たさない場合にテストを失敗させます。"""
    if not condition:
        raise AssertionError


class SlashCommandStructureTest(TestCase):
    def test_command_groups_match_the_documented_structure(self) -> None:
        actual = {group.name: {command.name for command in group.commands} for group in COMMAND_GROUPS}

        ensure(actual == EXPECTED_GROUP_COMMANDS)

    def test_independent_commands_remain_top_level(self) -> None:
        ensure({command.name for command in INDEPENDENT_COMMANDS} == {"choice", "hi", "remind"})
        ensure(all(isinstance(command, app_commands.Command) for command in INDEPENDENT_COMMANDS))

    def test_exposes_every_expected_slash_command(self) -> None:
        grouped_command_count = sum(len(commands) for commands in EXPECTED_GROUP_COMMANDS.values())

        ensure(grouped_command_count + len(INDEPENDENT_COMMANDS) == EXPECTED_SLASH_COMMAND_COUNT)
