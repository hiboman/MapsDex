import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, TextDisplay, TextInput
from django.utils import timezone

from ballsdex.core.discord import LayoutView, Modal
from bd_models.models import Report
from settings.models import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

REPORT_TYPES = [
    ("Report Violation", "violation"),
    ("Report Bug", "bug"),
    ("Provide Suggestion", "suggestion"),
    ("Other", "other"),
]


async def generate_report_id() -> str:
    while True:
        rid = str(random.randint(100000, 999999))
        if not await Report.objects.filter(report_id=rid).aexists():
            return rid


class ReportReplyModal(Modal, title="Reply to Report"):
    reply_field = TextInput(
        label="Reply Content",
        placeholder="Enter your reply...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, cog: "ReportCog", report: Report):
        super().__init__()
        self.cog = cog
        self.report = report

    async def on_submit(self, interaction: discord.Interaction["BallsDexBot"]):
        await interaction.response.defer(thinking=True)

        self.report.replied = True
        self.report.reply_content = self.reply_field.value
        self.report.reply_by = str(interaction.user)
        self.report.reply_time = timezone.now()
        await self.report.asave()

        if self.report.pk in self.cog.report_messages:
            original_message = self.cog.report_messages[self.report.pk]
            try:
                view = ReportAdminView(self.cog, self.report)
                view.status_display.content = (
                    f"## Report `{self.report.report_id}`\n"
                    f"**Type:** {self.report.get_report_type_display()}\n"
                    f"**From:** {self.report.user_name} (`{self.report.user_id}`)\n"
                    f"**Content:** {self.report.content}\n"
                    f"**Status:** Replied by {self.report.reply_by}"
                )
                for item in view.children:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True
                await original_message.edit(view=view)
            except discord.HTTPException:
                pass

        guild = self.cog.bot.get_guild(settings.report_guild_id or 0)
        if guild is not None:
            channel = guild.get_channel(settings.report_channel_id or 0)
            if channel and isinstance(channel, discord.TextChannel):
                reply_view = ReportReplyNotificationView(self.report, self.reply_field.value)
                await channel.send(view=reply_view)
                await interaction.followup.send("✅ Reply sent successfully.", ephemeral=True)

                try:
                    user = await self.cog.bot.fetch_user(self.report.user_id)
                    await user.send(
                        f"Hello, your report (ID: {self.report.report_id},"
                        f" Type: {self.report.get_report_type_display()}) has received an"
                        f" administrator reply:\n{self.reply_field.value}"
                    )
                except Exception:
                    pass
                return

        await interaction.followup.send("❌ Reply failed. Please contact an administrator.", ephemeral=True)


class ReportAdminView(LayoutView):
    """Components v2 view sent to the admin report channel for each new report."""

    status_display = TextDisplay("")
    reply_row = ActionRow()

    @reply_row.button(label="Reply", style=discord.ButtonStyle.primary)
    async def reply(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can use this feature.", ephemeral=True)
            return
        modal = ReportReplyModal(self.cog, self.report)
        await interaction.response.send_modal(modal)

    def __init__(self, cog: "ReportCog", report: Report):
        super().__init__()
        self.cog = cog
        self.report = report
        self.status_display.content = (
            f"## Report `{report.report_id}`\n"
            f"**Type:** {report.get_report_type_display()}\n"
            f"**From:** {report.user_name} (`{report.user_id}`)\n"
            f"**Content:** {report.content}\n"
            f"**Status:** Pending"
        )


class ReportReplyNotificationView(LayoutView):
    """Components v2 view sent to the report channel to confirm an admin reply."""

    content_display = TextDisplay("")

    def __init__(self, report: Report, reply_content: str):
        super().__init__()
        self.content_display.content = (
            f"## Reply Sent — Report `{report.report_id}`\n"
            f"**Type:** {report.get_report_type_display()}\n"
            f"**Original:** {report.content}\n"
            f"**Reply:** {reply_content}\n"
            f"**Replied by:** {report.reply_by}"
        )


class ReportCog(commands.Cog, name="Report"):
    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.report_messages: dict[int, discord.Message] = {}

    @app_commands.command(name="report", description="Report issues or suggestions to the backend server")
    @app_commands.describe(
        report_type="Select report type", content="Please describe your issue or suggestion in detail"
    )
    @app_commands.choices(report_type=[app_commands.Choice(name=label, value=value) for label, value in REPORT_TYPES])
    async def report(
        self, interaction: discord.Interaction["BallsDexBot"], report_type: app_commands.Choice[str], content: str
    ):
        report_id = await generate_report_id()
        report = await Report.objects.acreate(
            report_id=report_id,
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            report_type=report_type.value,
            content=content,
        )

        guild = self.bot.get_guild(settings.report_guild_id or 0)
        if guild is not None:
            channel = guild.get_channel(settings.report_channel_id or 0)
            if channel and isinstance(channel, discord.TextChannel):
                view = ReportAdminView(self, report)
                message = await channel.send(view=view)
                self.report_messages[report.pk] = message
                await interaction.response.send_message(
                    f"✅ Report submitted successfully! Your report ID is **{report_id}**."
                    " You will be notified of any updates via DM.",
                    ephemeral=True,
                )
                try:
                    await interaction.user.send(
                        f"Hello, we have received your report (ID: {report_id},"
                        f" Type: {report_type.name}). Our administrators will process it.\n"
                        "You will be notified of any updates via DM. Thank you for your assistance!"
                    )
                except Exception:
                    pass
                return

        await interaction.response.send_message(
            "❌ Report submission failed. Please contact an administrator.", ephemeral=True
        )
