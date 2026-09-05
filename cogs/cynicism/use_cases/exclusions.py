"""全メンバーが使える、冷笑ランキングの除外設定コマンド。"""

import discord
from discord.utils import escape_markdown

from cogs.cynicism.repositories.exclusions import CynicismExclusionRepository
from core.exception import ArgumentError

type ExclusionTarget = discord.TextChannel | discord.ForumChannel | discord.Thread
DESCRIPTION_LIMIT = 4096


class CynicismExclusionUseCases:
    """指定チャンネルだけの除外・解除・一覧表示を調整する。"""

    def __init__(self, repository: CynicismExclusionRepository) -> None:
        """永続化する除外設定リポジトリを保持する。"""
        self._repository = repository

    async def exclude(self, interaction: discord.Interaction, channel: ExclusionTarget | None) -> None:
        """指定先を除外する。省略時は実行場所を使い、管理者権限は要求しない。"""
        guild_id = self._require_guild(interaction)
        target = channel if channel is not None else interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
            message = "テキストチャンネル、フォーラム、スレッドを指定してください。"
            raise ArgumentError(message)
        if target.guild.id != guild_id:
            message = "このサーバーのチャンネルまたはスレッドを指定してください。"
            raise ArgumentError(message)
        await interaction.response.defer(thinking=True)
        await self._repository.exclude(guild_id, target.id, target.name)
        await interaction.followup.send(
            f"<#{target.id}> を冷笑ランキングの対象外にしました。配下のスレッドには適用しません。"
            "過去分を含む次の集計から反映されます。",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def include(self, interaction: discord.Interaction, channel_id: str | None) -> None:
        """選択した除外設定を解除する。保存済みIDから削除・アーカイブ後も解除できる。"""
        guild_id = self._require_guild(interaction)
        value = channel_id if channel_id is not None else str(interaction.channel_id or "")
        if value.startswith("<#") and value.endswith(">"):
            value = value[2:-1]
        if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**63:
            message = "除外一覧から選ぶか、チャンネル/スレッドのIDを指定してください。"
            raise ArgumentError(message)
        target_id = int(value)
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await self._repository.include(guild_id, target_id)
        notice = (
            f"<#{target_id}> の除外を解除しました。記録済みの過去分も次の集計から対象に戻ります。"
            if removed
            else f"<#{target_id}> は除外設定に登録されていません。"
        )
        await interaction.followup.send(notice, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def list_excluded(self, interaction: discord.Interaction) -> None:
        """全メンバーに、明示的に除外された対象の全件を確認可能にする。"""
        guild_id = self._require_guild(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        excluded = await self._repository.list_excluded(guild_id)
        if not excluded:
            await interaction.followup.send("除外設定はありません。", ephemeral=True)
            return
        pages: list[str] = []
        lines: list[str] = []
        for target in excluded:
            line = f"- <#{target.channel_id}>({escape_markdown(target.name)}) · ID: `{target.channel_id}`"
            if lines and len("\n".join([*lines, line])) > DESCRIPTION_LIMIT:
                pages.append("\n".join(lines))
                lines = []
            lines.append(line)
        pages.append("\n".join(lines))
        for index, page in enumerate(pages, start=1):
            embed = discord.Embed(title="冷笑ランキングの除外設定", description=page, color=discord.Color.blue())
            embed.set_footer(text=f"{index}/{len(pages)}ページ · 設定した対象のみ除外(配下のスレッドは含みません)")
            await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def autocomplete(self, interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
        """解除候補を保存済みの名前・IDで絞る。Discordの候補上限は25件。"""
        if interaction.guild_id is None:
            return []
        excluded = await self._repository.list_excluded(interaction.guild_id)
        return [
            discord.app_commands.Choice(name=f"{target.name} ({target.channel_id})"[:100], value=str(target.channel_id))
            for target in excluded
            if current.casefold() in target.name.casefold() or current in str(target.channel_id)
        ][:25]

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> int:
        """サーバー内での操作だけを許可する。"""
        if interaction.guild_id is None:
            message = "このコマンドはサーバー内で実行してください。"
            raise ArgumentError(message)
        return interaction.guild_id
