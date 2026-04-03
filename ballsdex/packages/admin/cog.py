import io
import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, Section, TextDisplay

from ballsdex.core.bot import impersonations
from ballsdex.core.discord import LayoutView
from ballsdex.core.utils import checks
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.menus import ItemFormatter, ListSource, Menu, dynamic_chunks, iter_to_async
from bd_models.models import GuildConfig
from settings.models import settings

from .balls import balls as balls_group
from .blacklist import blacklist as blacklist_group
from .blacklist import blacklistguild as blacklist_guild_group
from .flags import StatusFlags
from .history import history as history_group
from .info import info as info_group
from .logs import logs as logs_group
from .money import money as money_group

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.countryballs.cog import CountryBallsSpawner
    from ballsdex.packages.trade.cog import Trade

log = logging.getLogger("ballsdex.packages.admin")


class SyncView(LayoutView):
    def __init__(self, cog: "Admin", *, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog

    text = TextDisplay("Admin commands are already synced here. What would you like to do?")
    action_row = ActionRow()

    @action_row.button(
        label="Synchronize",
        style=discord.ButtonStyle.primary,
        emoji="\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}",
    )
    async def sync(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        assert interaction.guild
        self.stop()
        await interaction.response.defer()
        if not interaction.client.tree.get_command("admin", guild=interaction.guild):
            interaction.client.tree.add_command(self.cog.admin.app_command, guild=interaction.guild)
        await interaction.client.tree.sync(guild=interaction.guild)
        await GuildConfig.objects.aupdate_or_create(
            guild_id=interaction.guild.id, defaults={"guild_id": interaction.guild.id, "admin_command_synced": True}
        )
        self.sync.disabled = True
        self.remove.disabled = True
        self.text.content += (
            "\n\nCommands have been refreshed. You may need to reload your Discord client to see the changes applied."
        )
        await interaction.edit_original_response(view=self)

    @action_row.button(
        label="Remove", style=discord.ButtonStyle.danger, emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}"
    )
    async def remove(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        assert interaction.guild
        self.stop()
        await interaction.response.defer()
        interaction.client.tree.remove_command("admin", guild=interaction.guild)
        await interaction.client.tree.sync(guild=interaction.guild)
        await GuildConfig.objects.filter(guild_id=interaction.guild.id).aupdate(admin_command_synced=True)
        self.sync.disabled = True
        self.remove.disabled = True
        self.text.content += (
            "\n\nCommands have been removed. You may need to reload your Discord client to see the changes applied."
        )
        await interaction.edit_original_response(view=self)
        log.info(f"Admin commands removed from guild {interaction.guild.id} by {interaction.user}")


class Admin(commands.Cog):
    """
    Bot admin commands.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

        self.admin.add_command(info_group)
        self.admin.add_command(balls_group)
        self.admin.add_command(blacklist_group)
        self.admin.add_command(blacklist_guild_group)
        self.admin.add_command(history_group)
        self.admin.add_command(logs_group)
        self.admin.add_command(money_group)

    async def get_broadcast_channels(self):
        """Get all ball spawn channels for broadcasting"""
        try:
            channels = set()
            async for config in GuildConfig.objects.filter(enabled=True, spawn_channel__isnull=False):
                if not config.spawn_channel:
                    continue
                channel = self.bot.get_channel(config.spawn_channel)
                if channel:
                    channels.add(config.spawn_channel)
                else:
                    try:
                        config.enabled = False
                        await config.asave()
                    except Exception:
                        pass
            return channels
        except Exception as e:
            log.error(f"Error getting broadcast channels: {str(e)}")
            return set()

    async def cog_check(self, ctx: commands.Context["BallsDexBot"]) -> bool:
        return await checks.is_staff().predicate(ctx)

    async def cog_app_command_error(
        self, interaction: discord.Interaction["BallsDexBot"], error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandSignatureMismatch):
            assert self.bot.user
            await interaction.response.send_message(
                "Admin commands are desynchronized and needs to be re-synced. "
                f"Run `{self.bot.user.mention} admin syncslash` to fix this.",
                ephemeral=True,
            )
            interaction.extras["handled"] = True

    async def cog_load(self):
        guilds = [
            discord.Object(guild_id)
            async for guild_id in GuildConfig.objects.filter(admin_command_synced=True).values_list(
                "guild_id", flat=True
            )
        ]
        self.bot.tree.add_command(self.admin.app_command, guilds=guilds)

    @commands.hybrid_group()
    @app_commands.guilds(0)
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @checks.is_staff()
    async def admin(self, ctx: commands.Context):
        """
        Bot admin commands.
        """
        await ctx.send_help(ctx.command)

    @admin.command(with_app_command=False)
    @commands.is_owner()
    @commands.guild_only()
    async def syncslash(self, ctx: commands.Context["BallsDexBot"]):
        """
        Synchronize all the admin commands in the current server, or remove them if already existing.
        """
        assert ctx.guild
        commands = await self.bot.tree.fetch_commands(guild=ctx.guild)
        if commands:
            view = SyncView(self)
            await ctx.send(view=view)
        else:
            view = ConfirmChoiceView(ctx, accept_message="Registering commands...")
            await ctx.send(
                "Would you like to add admin slash commands in this server? "
                "They can only be used with the appropriate Django permissions",
                view=view,
            )
            await view.wait()
            if not view.value:
                return
            async with ctx.typing():
                self.bot.tree.add_command(self.admin.app_command, guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                log.info(f"Admin commands added to guild {ctx.guild.id} by {ctx.author}")
                await ctx.send(
                    "Admin slash commands added.\nYou need admin permissions in this server to view them "
                    f"(this can be changed [here](discord://-/guilds/{ctx.guild.id}/settings/integrations)). You might "
                    "need to refresh your Discord client to view them."
                )
                await GuildConfig.objects.aupdate_or_create(
                    guild_id=ctx.guild.id, defaults={"guild_id": ctx.guild.id, "admin_command_synced": True}
                )

    @admin.command()
    @checks.is_superuser()
    async def status(self, ctx: commands.Context["BallsDexBot"], *, flags: StatusFlags):
        """
        Change the status of the bot. Provide at least status or text.
        """
        if not flags.status and not flags.name and not flags.state:
            await ctx.send("You must provide at least `status`, `name` or `state`.", ephemeral=True)
            return

        activity: discord.Activity | None = None
        if flags.activity_type == discord.ActivityType.custom and flags.name and not flags.state:
            await ctx.send("You must provide `state` for custom activities. `name` is unused.", ephemeral=True)
            return
        if flags.activity_type != discord.ActivityType.custom and not flags.name:
            await ctx.send("You must provide `name` for pre-defined activities.", ephemeral=True)
            return
        if flags.name or flags.state:
            activity = discord.Activity(name=flags.name or flags.state, state=flags.state, type=flags.activity_type)
        await self.bot.change_presence(status=flags.status, activity=activity)
        await ctx.send("Status updated.", ephemeral=True)

    @admin.command()
    @checks.is_superuser()
    async def trade_lockdown(self, ctx: commands.Context["BallsDexBot"], *, reason: str):
        """
        Cancel all ongoing trades and lock down further trades from being started.

        Parameters
        ----------
        reason: str
            The reason of the lockdown. This will be displayed to all trading users.
        """
        cog = cast("Trade | None", self.bot.get_cog("Trade"))
        if not cog:
            await ctx.send("The trade cog is not loaded.", ephemeral=True)
            return

        await ctx.defer()
        result = await cog.cancel_all_trades(reason)

        assert self.bot.user
        prefix = settings.prefix if self.bot.intents.message_content else f"{self.bot.user.mention} "

        if not result:
            await ctx.send(
                "All trades were successfully cancelled, and further trades cannot be started "
                f'anymore.\nTo enable trades again, the bot owner must use the "{prefix}reload '
                'trade" command.'
            )
        else:
            await ctx.send(
                "Lockdown mode enabled, trades can no longer be started. "
                f"While cancelling ongoing trades, {len(result)} failed to cancel, check your "
                "logs for info.\nTo enable trades again, the bot owner must use the "
                f'"{prefix}reload trade" command.'
            )

    @admin.command()
    @checks.is_superuser()
    async def cooldown(self, ctx: commands.Context["BallsDexBot"], guild_id: str | None = None):
        """
        Show the details of the spawn cooldown system for the given server

        Parameters
        ----------
        guild_id: int | None
            ID of the server you want to inspect. If not given, inspect the current server.
        """
        if guild_id:
            try:
                guild = self.bot.get_guild(int(guild_id))
            except ValueError:
                await ctx.send("Invalid guild ID. Please make sure it's a number.", ephemeral=True)
                return
        else:
            guild = ctx.guild
        if not guild:
            await ctx.send("The given guild could not be found.", ephemeral=True)
            return

        spawn_manager = cast("CountryBallsSpawner", self.bot.get_cog("CountryBallsSpawner")).spawn_manager
        await spawn_manager.admin_explain(ctx, guild)

    @admin.command()
    async def guilds(self, ctx: commands.Context["BallsDexBot"], user: discord.User):
        """
        Shows the guilds shared with the specified user. Provide either user or user_id.

        Parameters
        ----------
        user: discord.User
            The user you want to check, if available in the current server.
        """
        if self.bot.intents.members:
            guilds = user.mutual_guilds
        else:
            guilds = [x for x in self.bot.guilds if x.owner_id == user.id]

        if not guilds:
            if self.bot.intents.members:
                await ctx.send(f"The user does not own any server with {settings.bot_name}.", ephemeral=True)
            else:
                await ctx.send(
                    f"The user does not own any server with {settings.bot_name}.\n"
                    ":warning: *The bot cannot be aware of the member's presence in servers, "
                    "it is only aware of server ownerships.*",
                    ephemeral=True,
                )
            return

        entries: list[TextDisplay] = []
        for guild in guilds:
            if config := await GuildConfig.objects.aget_or_none(guild_id=guild.id):
                spawn_enabled = config.enabled and config.guild_id
            else:
                spawn_enabled = False

            text = f"## {guild.name} - `{guild.id}`\n"

            # highlight suspicious server names
            if any(x in guild.name.lower() for x in ("farm", "grind", "spam")):
                text += f"- :warning: **{guild.name}**\n"
            else:
                text += f"- {guild.name}\n"

            # highlight low member count
            if guild.member_count <= 15:  # type: ignore
                text += f"- :warning: **{guild.member_count} members**\n"
            else:
                text += f"- {guild.member_count} members\n"

            # highlight if spawning is enabled
            if spawn_enabled:
                text += "- :warning: **Spawn is enabled**"
            else:
                text += "- Spawn is disabled"

            entries.append(TextDisplay(text))

        view = LayoutView()
        container = Container()
        view.add_item(container)
        section = Section(
            TextDisplay(f"## {len(guilds)} servers shared"),
            TextDisplay(f"{user.mention} ({user.id})"),
            accessory=Button(
                style=discord.ButtonStyle.link,
                label="View profile",
                url=f"discord://-/users/{user.id}",
                emoji="\N{LEFT-POINTING MAGNIFYING GLASS}",
            ),
        )
        container.add_item(section)

        if not self.bot.intents.members:
            section.add_item(
                TextDisplay(
                    "\N{WARNING SIGN} The bot cannot be aware of the member's "
                    "presence in servers, it is only aware of server ownerships."
                )
            )

        pages = Menu(
            self.bot, view, ListSource(await dynamic_chunks(view, iter_to_async(entries))), ItemFormatter(container, 1)
        )
        await pages.init()
        await ctx.send(view=view, ephemeral=True)

    @admin.command()
    @checks.is_superuser()
    async def impersonate(self, ctx: commands.Context["BallsDexBot"], user: discord.Member | None = None):
        """
        Impersonate a user on your next slash commands.

        Run this command without parameters to clear impersonation.

        Parameters
        ----------
        user: discord.Member
            The user to impersonate
        """
        if user is None:
            if ctx.author.id not in impersonations:
                await ctx.send_help(ctx.command)
                return
            del impersonations[ctx.author.id]
            await ctx.send("You are not impersonating anymore.")
        else:
            impersonations[ctx.author.id] = user
            await ctx.send(
                f"Your next commands will be run as if {user.display_name} ran it.\n"
                "Avoid running the commands in a different server, this can lead to weird issues.\n"
                f"To clear impersonation, run `{ctx.prefix}admin impersonate` again.",
                ephemeral=True,
            )

    @admin.command()
    @checks.is_superuser()
    async def say(
        self,
        ctx: commands.Context["BallsDexBot"],
        message: str,
        channel: discord.TextChannel | discord.Thread | None = None,
    ):
        """
        Send a message as the bot to a specified channel.

        Parameters
        ----------
        message: str
            The message to send
        channel: discord.TextChannel | discord.Thread | None
            The channel or thread to send the message to. Defaults to current channel if not specified.
        """
        # Get target channel
        target_channel = channel if channel else ctx.channel

        # Verify it's a valid messageable channel/thread
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await ctx.send("Invalid channel type. Must be a text channel or thread.", ephemeral=True)
            return

        try:
            await target_channel.send(message)

            # Get guild name safely (threads have parent channels)
            guild_name = target_channel.guild.name if hasattr(target_channel, "guild") else "Unknown"

            await ctx.send(f"Message sent to {target_channel.mention} ({guild_name})", ephemeral=True)
        except discord.Forbidden:
            await ctx.send(f"Missing permissions to send messages in {target_channel.mention}", ephemeral=True)
        except Exception as e:
            await ctx.send(f"Failed to send message: {str(e)}", ephemeral=True)

    @admin.command(name="broadcast", description="Send a broadcast message to all ball spawn channels")
    @checks.is_superuser()
    @app_commands.choices(
        broadcast_type=[
            app_commands.Choice(name="Text and Image", value="both"),
            app_commands.Choice(name="Text Only", value="text"),
            app_commands.Choice(name="Image Only", value="image"),
        ]
    )
    async def broadcast(
        self,
        ctx: commands.Context["BallsDexBot"],
        broadcast_type: str,
        message: str | None = None,
        attachment: discord.Attachment | None = None,
        anonymous: bool = False,
    ):
        """Send broadcast messages to all ball spawn channels"""
        if broadcast_type == "text" and not message:
            await ctx.send("You must provide a message when selecting 'Text Only' mode.", ephemeral=True)
            return
        if broadcast_type == "image" and not attachment:
            await ctx.send("You must provide an image when selecting 'Image Only' mode.", ephemeral=True)
            return
        if broadcast_type == "both" and not message and not attachment:
            await ctx.send("You must provide a message or image when selecting 'Text and Image' mode.", ephemeral=True)
            return

        try:
            channels = await self.get_broadcast_channels()
            if not channels:
                await ctx.send("No ball spawn channels are currently configured.", ephemeral=True)
                return

            await ctx.send("Broadcasting message...", ephemeral=True)

            success_count = 0
            fail_count = 0
            failed_channels = []

            broadcast_message = None
            if message:
                broadcast_message = (
                    f"🔔 **System Announcement** 🔔\n------------------------\n{message}\n------------------------\n"
                )
                if not anonymous:
                    broadcast_message += f"*Sent by {ctx.author.name}*"

            file_data = None
            if attachment and broadcast_type in ["both", "image"]:
                try:
                    file_data = await attachment.read()
                except Exception as e:
                    log.error(f"Error downloading attachment: {str(e)}")
                    await ctx.send(
                        "An error occurred while downloading the attachment. Only the text message will be sent.",
                        ephemeral=True,
                    )

            for channel_id in channels:
                try:
                    channel = self.bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        if broadcast_type == "text":
                            await channel.send(broadcast_message)
                        elif broadcast_type == "image" and file_data and attachment:
                            new_file = discord.File(
                                io.BytesIO(file_data), filename=attachment.filename, spoiler=attachment.is_spoiler()
                            )
                            await channel.send(file=new_file)
                        else:  # both
                            if file_data and broadcast_message and attachment:
                                new_file = discord.File(
                                    io.BytesIO(file_data), filename=attachment.filename, spoiler=attachment.is_spoiler()
                                )
                                await channel.send(broadcast_message, file=new_file)
                            elif file_data and attachment:
                                new_file = discord.File(
                                    io.BytesIO(file_data), filename=attachment.filename, spoiler=attachment.is_spoiler()
                                )
                                await channel.send(file=new_file)
                            elif broadcast_message:
                                await channel.send(broadcast_message)
                        success_count += 1
                    else:
                        fail_count += 1
                        failed_channels.append(f"Channel ID: {channel_id}")
                except Exception as e:
                    log.error(f"Error sending to channel {channel_id}: {str(e)}")
                    fail_count += 1
                    failed_channels.append(f"Channel ID: {channel_id}")

            result_message = (
                f"Broadcast complete!\nSuccessfully sent to: {success_count} channels\nFailed: {fail_count} channels"
            )
            if failed_channels:
                result_message += "\n\nFailed channels:\n" + "\n".join(failed_channels[:10])
                if len(failed_channels) > 10:
                    result_message += f"\n... and {len(failed_channels) - 10} more"

            await ctx.send(result_message, ephemeral=True)

        except Exception as e:
            log.error(f"Error in broadcast: {str(e)}")
            await ctx.send("An error occurred while executing the command. Please try again later.", ephemeral=True)

    @admin.command(name="broadcast_dm", description="Send a DM broadcast to specific users")
    @checks.is_superuser()
    async def broadcast_dm(
        self, ctx: commands.Context["BallsDexBot"], message: str, user_ids: str, anonymous: bool = False
    ):
        """Private Message Broadcasting to Specified users

        Args:
            message: the message you are going to send
            user_ids: a comma-separated list of user IDs to send the message to
            anonymous: gives an option to send the message anonymously
        """
        try:
            user_id_list = [uid.strip() for uid in user_ids.split(",")]
            if not user_id_list:
                await ctx.send("Please provide at least one user ID.", ephemeral=True)
                return

            await ctx.send("Starting DM broadcast...", ephemeral=True)

            success_count = 0
            fail_count = 0
            failed_users = []

            dm_message = f"🔔 **System DM** 🔔\n------------------------\n{message}\n------------------------\n"
            if not anonymous:
                dm_message += f"*Sent by {ctx.author.name}*"

            for user_id in user_id_list:
                try:
                    user = await self.bot.fetch_user(int(user_id))
                    if user:
                        await user.send(dm_message)
                        success_count += 1
                    else:
                        fail_count += 1
                        failed_users.append(f"Unknown User (ID: {user_id})")
                except Exception as e:
                    log.error(f"Error sending DM to user {user_id}: {str(e)}")
                    fail_count += 1
                    failed_users.append(f"User ID: {user_id}")

            result_message = (
                f"DM broadcast complete!\nSuccessfully sent: {success_count} users\nFailed: {fail_count} users"
            )
            if failed_users:
                result_message += "\n\nFailed users:\n" + "\n".join(failed_users[:10])
                if len(failed_users) > 10:
                    result_message += f"\n... and {len(failed_users) - 10} more"

            await ctx.send(result_message, ephemeral=True)

        except Exception as e:
            log.error(f"Error in broadcast_dm: {str(e)}")
            try:
                await ctx.send("An error occurred while executing the command. Please try again later.", ephemeral=True)
            except Exception:
                pass
