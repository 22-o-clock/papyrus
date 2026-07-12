import re
from logging import getLogger
from secrets import choice

from discord import Interaction, app_commands
from discord.ext import commands

logger = getLogger(__name__)

SPEAK_TABLE = [
    "{0}さん、こんにちは！今日はどんな楽しいことがありましたか？",
    "{0}さん、お疲れ様です♪何かお手伝いできることありますか？",
    "{0}さん、元気ですか？お話ししましょう！",
    "{0}さん、お茶でもしながら話しませんか？",
    "{0}さん、最近ハマってることって何ですか？",
    "{0}さん、いつも頑張ってますね！応援してますよ！",
    "{0}さん、お腹空いてませんか？何かおいしいものでも食べましょう。",
    "{0}さん、今日の天気はどうでしたか？散歩に行きましたか？",
    "{0}さん、最近あった面白いことを教えてください。",
    "こんにちは、{0}さん。{1}です。よろしくお願いします。",
    "やあ、{0}さん。今日はどんなことをしますか？",
    "お手伝いが必要なら、{1}にお任せください。",
    "おはようございます、{0}さん。今日も一緒に頑張りましょう。",
    "{0}さん、何かお話ししたいことがありますか？",
    "{1}に質問があれば、どうぞ気軽に聞いてください。",
    "{1}がここにいますよ。何かお手伝いできることはありますか？",
    "こんにちは、{0}さん。{1}と一緒に楽しい時間を過ごしましょう。",
    "{1}です。何かお手伝いできることがあれば、教えてください。",
    "{0}さん、{1}と一緒に今日も頑張りましょう。",
    "{0}さん、最近読んだ本とかありますか？教えてください！",
    "{0}さん、今日のファッション素敵ですね♡",
    "{0}さん、一緒に映画見ましょう！何がいいかな？",
    "{0}さん、最近の音楽で好きな曲ありますか？",
    "{0}さん、何か悩み事があったら聞かせてくださいね。",
    "{0}さん、今日はどんなことをしましたか？",
    "{0}さん、好きな食べ物は何ですか？私も知りたいな！",
    "{0}さん、今度一緒に遊びに行きませんか？",
    "{0}さん、今日はどんな気分ですか？",
    "{0}さん、いつもありがとう！感謝してます♡",
    "{0}さん、今日はどんなことに挑戦しましたか？",
    "{0}さん、最近読んだ本や見た映画でおすすめはありますか？",
    "{0}さん、今日は何か特別なことがありましたか？",
    "{0}さん、最近の趣味や興味を教えてください。",
    "{0}さん、何か相談したいことがあればどうぞ。",
    "{0}さん、今日はどんな気分ですか？",
    "{0}さん、週末の予定は決まりましたか？",
    "{0}さん、最近行った場所で面白かったところはどこですか？",
    "{0}さん、何か新しいことを始めましたか？",
    "{0}さん、今日はどんな音楽を聴いていますか？",
]


class Speak(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        logger.info("%s.on_ready() is called.", __name__)

    @app_commands.command(name="hi", description="ボットがあなたに挨拶します。")
    async def hi(self, interaction: Interaction) -> None:
        """ボットがあなたに挨拶します。"""
        if self.bot.user is None:
            logger.error("Unexpected type of self.bot.user: expected ClientUser, got NoneType.")
            return

        await interaction.response.send_message(choice(SPEAK_TABLE).format(interaction.user.mention, self.bot.user.name))

    @app_commands.command(name="choice", description="選択肢の中から1つをランダムに選びます。")
    @app_commands.describe(option="選択肢を空白区切りで入力")
    async def choice_command(self, interaction: Interaction, option: str) -> None:
        """指定した選択肢の中から、ボットが1つをランダムに選びます。"""
        match = re.findall(r"\S+", option)
        if len(match) == 1:
            await interaction.response.send_message(
                "選択肢は空白区切りで複数入力できます。選択肢は複数必要です。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(f"じゃあ... {choice(match)} で決定！")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Speak(bot))
    logger.debug("%s is added to the bot.", __name__)
