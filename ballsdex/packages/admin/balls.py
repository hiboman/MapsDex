import asyncio
import logging
import random
import re
from collections import defaultdict
from datetime import datetime
from itertools import cycle
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands
from discord.utils import format_dt
from django.urls import reverse

from ballsdex.core.bot import BallsDexBot
from ballsdex.core.utils import checks
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.menus import Menu, TextFormatter, TextSource
from ballsdex.core.utils.transformers import SpecialTransform
from bd_models.models import (
    Ball,
    BallInstance,
    BlacklistedGuild,
    BlacklistHistory,
    GuildConfig,
    Player,
    Special,
    TradeObject,
)
from bd_models.models import balls as balls_cache
from settings.models import settings

from .flags import BallsCountFlags, GiveBallFlags, RarityFlags, SpawnFlags

if TYPE_CHECKING:
    from ballsdex.packages.countryballs.cog import CountryBallsSpawner
    from ballsdex.packages.countryballs.countryball import BallSpawnView

log = logging.getLogger("ballsdex.packages.admin.balls")
FILENAME_RE = re.compile(r"^(.+)(\.\S+)$")


async def save_file(attachment: discord.Attachment) -> Path:
    path = Path(f"./admin_panel/media/{attachment.filename}")
    match = FILENAME_RE.match(attachment.filename)
    if not match:
        raise TypeError("The file you uploaded lacks an extension.")
    i = 1
    while path.exists():
        path = Path(f"./admin_panel/media/{match.group(1)}-{i}{match.group(2)}")
        i = i + 1
    await attachment.save(path)
    return path.relative_to("./admin_panel/media/")


async def _spawn_bomb(
    ctx: commands.Context[BallsDexBot],
    countryball_cls: type["BallSpawnView"],
    countryball: Ball | None,
    channel: discord.TextChannel,
    n: int,
    special: Special | None = None,
    atk_bonus: int | None = None,
    hp_bonus: int | None = None,
    filtered_balls: list[Ball] | None = None,
):
    spawned = 0
    message: discord.Message

    async def update_message_loop():
        nonlocal spawned, message
        for i in range(5 * 12 * 10):  # timeout progress after 10 minutes
            await edit_func(
                content=f"Spawn bomb in progress in {channel.mention}, "
                f"{settings.collectible_name.title()}: {countryball or 'Random'}\n"
                f"{spawned}/{n} spawned ({round((spawned / n) * 100)}%)"
            )
            await asyncio.sleep(5)
        await edit_func(content="Spawn bomb seems to have timed out.")

    message = await ctx.send(f"Starting spawn bomb in {channel.mention}...", ephemeral=True)
    edit_func = ctx.interaction.edit_original_response if ctx.interaction else message.edit
    task = ctx.bot.loop.create_task(update_message_loop())
    try:
        for i in range(n):
            if not countryball:
                if filtered_balls:
                    ball_model = random.choice(filtered_balls)
                    ball = countryball_cls(ctx.bot, ball_model)
                else:
                    ball = await countryball_cls.get_random(ctx.bot)
            else:
                ball = countryball_cls(ctx.bot, countryball)
            ball.special = special
            ball.atk_bonus = atk_bonus
            ball.hp_bonus = hp_bonus
            result = await ball.spawn(channel)
            if not result:
                task.cancel()
                await edit_func(
                    content=f"A {settings.collectible_name} failed to spawn, probably "
                    "indicating a lack of permissions to send messages "
                    f"or upload files in {channel.mention}."
                )
                return
            spawned += 1
        task.cancel()
        await edit_func(
            content=f"Successfully spawned {spawned} {settings.plural_collectible_name} in {channel.mention}!"
        )
    finally:
        task.cancel()


@commands.hybrid_group(name=settings.balls_slash_name)
async def balls(ctx: commands.Context[BallsDexBot]):
    """
    Countryballs management
    """
    await ctx.send_help(ctx.command)


@balls.command()
@commands.cooldown(1, 120, commands.BucketType.user)
@checks.has_permissions("bd_models.add_ballinstance")
async def spawn(ctx: commands.Context[BallsDexBot], *, flags: SpawnFlags):
    """
    Force spawn a random or specified countryball.
    """
    # the transformer triggered a response, meaning user tried an incorrect input
    cog = cast("CountryBallsSpawner | None", ctx.bot.get_cog("CountryBallsSpawner"))
    if not cog:
        prefix = settings.prefix if ctx.bot.intents.message_content or not ctx.bot.user else f"{ctx.bot.user.mention} "
        # do not replace `countryballs` with `settings.collectible_name`, it is intended
        await ctx.send(
            "The `countryballs` package is not loaded, this command is unavailable.\n"
            "Please resolve the errors preventing this package from loading. Use "
            f'"{prefix}reload countryballs" to try reloading it.',
            ephemeral=True,
        )
        return

    # Parse and validate tier_range parameter
    filtered_balls = None
    if flags.tier_range:
        if flags.countryball:
            await ctx.send(
                "The `tier_range` parameter can only be used with random spawns, not with a specific countryball.",
                ephemeral=True,
            )
            return

        if "-" not in flags.tier_range:
            await ctx.send("Invalid tier_range format. Use format like '2-8' for T2 to T8.", ephemeral=True)
            return

        try:
            parts = flags.tier_range.split("-")
            if len(parts) != 2:
                raise ValueError
            min_tier = int(parts[0])
            max_tier = int(parts[1])
        except ValueError:
            await ctx.send("Invalid tier_range format. Use format like '2-8' for T2 to T8.", ephemeral=True)
            return

        enabled_collectibles = [x for x in balls_cache.values() if x.enabled]
        rarity_to_collectibles = {}
        for c in enabled_collectibles:
            rarity_to_collectibles.setdefault(c.rarity, []).append(c)

        sorted_rarities = sorted(rarity_to_collectibles.keys())

        if min_tier < 1 or max_tier > len(sorted_rarities) or min_tier > max_tier:
            await ctx.send(
                f"Invalid tier range. Must be between T1-T{len(sorted_rarities)} and min <= max.", ephemeral=True
            )
            return

        filtered_balls = []
        for tier_idx in range(min_tier - 1, max_tier):
            rarity = sorted_rarities[tier_idx]
            filtered_balls.extend(rarity_to_collectibles[rarity])

        if not filtered_balls:
            await ctx.send(
                f"No {settings.plural_collectible_name} found in tier range T{min_tier}-T{max_tier}.", ephemeral=True
            )
            return

    special_attrs = []
    if flags.special is not None:
        special_attrs.append(f"special={flags.special.name}")
    if flags.atk_bonus is not None:
        special_attrs.append(f"atk={flags.atk_bonus}")
    if flags.hp_bonus is not None:
        special_attrs.append(f"hp={flags.hp_bonus}")
    if flags.tier_range:
        special_attrs.append(f"tier_range={flags.tier_range}")

    if flags.n > 1:
        await _spawn_bomb(
            ctx,
            cog.countryball_cls,
            flags.countryball,
            flags.channel or ctx.channel,  # type: ignore
            flags.n,
            flags.special,
            flags.atk_bonus,
            flags.hp_bonus,
            filtered_balls,
        )
        log.info(
            f"{ctx.author} spawned {settings.collectible_name}"
            f" {flags.countryball or 'random'} {flags.n} times in {flags.channel or ctx.channel}"
            + (f" ({', '.join(special_attrs)})." if special_attrs else "."),
            extra={"webhook": True},
        )
        return

    await ctx.defer(ephemeral=True)
    if not flags.countryball:
        if filtered_balls:
            ball_model = random.choice(filtered_balls)
            ball = cog.countryball_cls(ctx.bot, ball_model)
        else:
            ball = await cog.countryball_cls.get_random(ctx.bot)
    else:
        ball = cog.countryball_cls(ctx.bot, flags.countryball)
    ball.special = flags.special
    ball.atk_bonus = flags.atk_bonus
    ball.hp_bonus = flags.hp_bonus
    result = await ball.spawn(flags.channel or ctx.channel)  # type: ignore

    if result:
        await ctx.send(f"{settings.collectible_name.title()} spawned.", ephemeral=True)
        log.info(
            f"{ctx.author} spawned {settings.collectible_name} {ball.name} "
            f"in {flags.channel or ctx.channel}" + (f" ({', '.join(special_attrs)})." if special_attrs else "."),
            extra={"webhook": True},
        )


@balls.command()
@checks.has_permissions("bd_models.add_ballinstance")
async def give(ctx: commands.Context[BallsDexBot], user: discord.User, *, flags: GiveBallFlags):
    """
    Give the specified countryball to a player.

    Parameters
    ----------
    user: discord.User
        The user you want to give a countryball to
    """
    await ctx.defer(ephemeral=True)

    player, created = await Player.objects.aget_or_create(discord_id=user.id)
    instances = []
    for _ in range(flags.n):
        instance = await BallInstance.objects.acreate(
            ball=flags.countryball,
            player=player,
            attack_bonus=(
                flags.attack_bonus
                if flags.attack_bonus is not None
                else random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
            ),
            health_bonus=(
                flags.health_bonus
                if flags.health_bonus is not None
                else random.randint(-settings.max_health_bonus, settings.max_health_bonus)
            ),
            special=flags.special,
        )
        instances.append(instance)
    if flags.n == 1:
        instance = instances[0]
        await ctx.send(
            f"`{flags.countryball.country}` (`{instance.pk:0X}`) "
            f"{settings.collectible_name} was successfully given to "
            f"`{user}`.\nSpecial: `{flags.special.name if flags.special else None}` • ATK: "
            f"`{instance.attack_bonus:+d}` • HP:`{instance.health_bonus:+d}` "
        )
        log.info(
            f"{ctx.author} gave {settings.collectible_name} {flags.countryball.country} (`{instance.pk:0X}`) "
            f"to {user}. (Special={flags.special.name if flags.special else None} "
            f"ATK={instance.attack_bonus:+d} HP={instance.health_bonus:+d}).",
            extra={"webhook": True},
        )
    else:
        bonus_info = ""
        if flags.attack_bonus is not None or flags.health_bonus is not None:
            atk_str = f"ATK={flags.attack_bonus:+d}" if flags.attack_bonus is not None else "ATK=random"
            hp_str = f"HP={flags.health_bonus:+d}" if flags.health_bonus is not None else "HP=random"
            bonus_info = f"\n{atk_str} • {hp_str}"
        await ctx.send(
            f"`{flags.n}x {flags.countryball.country}` "
            f"{settings.plural_collectible_name} were successfully given to "
            f"`{user}`.\nSpecial: `{flags.special.name if flags.special else None}`{bonus_info}"
        )
        log_bonus = ""
        if flags.attack_bonus is not None or flags.health_bonus is not None:
            atk_log = f"ATK={flags.attack_bonus:+d}" if flags.attack_bonus is not None else "ATK=random"
            hp_log = f"HP={flags.health_bonus:+d}" if flags.health_bonus is not None else "HP=random"
            log_bonus = f" {atk_log} {hp_log}"
        log.info(
            f"{ctx.author} gave {flags.n}x {settings.collectible_name} {flags.countryball.country} "
            f"to {user}. (Special={flags.special.name if flags.special else None}{log_bonus}).",
            extra={"webhook": True},
        )


@balls.command(name="info")
@checks.has_permissions("bd_models.view_ballinstance")
async def balls_info(ctx: commands.Context[BallsDexBot], countryball_id: str):
    """
    Show information about a countryball.

    Parameters
    ----------
    countryball_id: str
        The ID of the countryball you want to get information about.
    """
    try:
        pk = int(countryball_id, 16)
    except ValueError:
        await ctx.send(f"The {settings.collectible_name} ID you gave is not valid.", ephemeral=True)
        return
    try:
        ball = await BallInstance.objects.prefetch_related("player", "trade_player", "special").aget(id=pk)
    except BallInstance.DoesNotExist:
        await ctx.send(f"The {settings.collectible_name} ID you gave does not exist.", ephemeral=True)
        return
    spawned_time = format_dt(ball.spawned_time, style="R") if ball.spawned_time else "N/A"
    catch_time = (
        (ball.catch_date - ball.spawned_time).total_seconds() if ball.catch_date and ball.spawned_time else "N/A"
    )
    admin_url = f"[View online](<{reverse('admin:bd_models_ballinstance_change', args=(ball.pk,))}>)"
    await ctx.send(
        f"**{settings.collectible_name.title()} ID:** {ball.pk}\n"
        f"**Player:** {ball.player}\n"
        f"**Name:** {ball.countryball}\n"
        f"**Attack:** {ball.attack}\n"
        f"**Attack bonus:** {ball.attack_bonus}\n"
        f"**Health bonus:** {ball.health_bonus}\n"
        f"**Health:** {ball.health}\n"
        f"**Special:** {ball.special.name if ball.special else None}\n"
        f"**Caught at:** {format_dt(ball.catch_date, style='R')}\n"
        f"**Spawned at:** {spawned_time}\n"
        f"**Catch time:** {catch_time} seconds\n"
        f"**Caught in:** {ball.server_id if ball.server_id else 'N/A'}\n"
        f"**Traded:** {ball.trade_player}\n{admin_url}",
        ephemeral=True,
    )
    log.info(f"{ctx.author} got info for {ball}({ball.pk}).", extra={"webhook": True})


@balls.command(name="delete")
@checks.has_permissions("bd_models.delete_ballinstance")
async def balls_delete(ctx: commands.Context[BallsDexBot], countryball_id: str, soft_delete: bool = True):
    """
    Delete a countryball.

    Parameters
    ----------
    countryball_id: str
        The ID of the countryball you want to delete.
    soft_delete: bool
        Whether the countryball should be kept in database or fully wiped.
    """
    try:
        ballIdConverted = int(countryball_id, 16)
    except ValueError:
        await ctx.send(f"The {settings.collectible_name} ID you gave is not valid.", ephemeral=True)
        return
    try:
        ball = await BallInstance.objects.aget(id=ballIdConverted)
    except BallInstance.DoesNotExist:
        await ctx.send(f"The {settings.collectible_name} ID you gave does not exist.", ephemeral=True)
        return
    if soft_delete:
        ball.deleted = True
        await ball.asave()
        await ctx.send(f"{settings.collectible_name.title()} {countryball_id} soft deleted.", ephemeral=True)
        log.info(f"{ctx.author} soft deleted {ball}({ball.pk}).", extra={"webhook": True})
    else:
        await ball.adelete()
        await ctx.send(f"{settings.collectible_name.title()} {countryball_id} hard deleted.", ephemeral=True)
        log.info(f"{ctx.author} hard deleted {ball}({ball.pk}).", extra={"webhook": True})


@balls.command(name="transfer")
@checks.has_permissions("bd_models.change_ballinstance")
async def balls_transfer(
    ctx: commands.Context[BallsDexBot],
    user_1: discord.User,
    user_2: discord.User | None = None,
    countryball_id: str | None = None,
    percentage: int | None = None,
    users: str | None = None,
    special: SpecialTransform | None = None,
    clean_balls: bool = False,
    trade_player_id: int | None = None,
):
    """
    Transfer countryballs between users.

    Parameters
    ----------
    user_1: discord.User
        When using countryball_id: The user to transfer the countryball to (recipient).
        When using percentage: The user to transfer countryballs from (source).
    user_2: discord.User | None
        The user to transfer countryballs to. Required when using percentage without users.
    countryball_id: str | None
        The ID(s) of the countryball you want to transfer. Can be comma-separated (e.g., "ABC, DEF, 123").
    percentage: int | None
        The percentage of countryballs to transfer. Can be combined with users for pool distribution.
    users: str | None
        Comma-separated user IDs for pool distribution (e.g., "123, 456, 789").
    special: Special | None
        Filter by special when using percentage transfer.
    clean_balls: bool
        If True, removes trade player, spawn date, catch time, trade history, and server ID.
    trade_player_id: int | None
        Discord user ID to set as the trade player for transferred balls. Only works with clean_balls=True.
    """
    if countryball_id and (percentage or users):
        await ctx.send("Cannot use countryball_id with percentage or users.", ephemeral=True)
        return

    if not countryball_id and not percentage:
        await ctx.send("Either countryball_id or percentage must be provided.", ephemeral=True)
        return

    if percentage and not (1 <= percentage <= 100):
        await ctx.send("Percentage must be between 1 and 100.", ephemeral=True)
        return

    if trade_player_id and not clean_balls:
        await ctx.send("trade_player_id can only be used when clean_balls is True.", ephemeral=True)
        return

    pool_user_ids = []
    if users:
        try:
            pool_user_ids = [int(uid.strip()) for uid in users.split(",")]
            if len(pool_user_ids) < 2:
                raise ValueError("Need at least 2 users")
        except ValueError:
            await ctx.send(
                "Invalid users format. Use comma-separated IDs (e.g., '123, 456, 789') with at least 2 users.",
                ephemeral=True,
            )
            return

    if percentage and not users and not user_2:
        await ctx.send("user_2 required when using percentage without users.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    if percentage:
        try:
            player_1 = await Player.objects.aget(discord_id=user_1.id)
        except Player.DoesNotExist:
            await ctx.send(f"{user_1} does not exist or has no {settings.plural_collectible_name}.", ephemeral=True)
            return

        filters = {"player": player_1, "deleted": False}
        if special:
            filters["special"] = special

        balls = [b async for b in BallInstance.objects.filter(**filters)]
        if not balls:
            special_text = f" with special {special.name}" if special else ""
            await ctx.send(
                f"{user_1} has no {settings.plural_collectible_name}{special_text} to transfer.", ephemeral=True
            )
            return

        to_transfer = balls if percentage == 100 else random.sample(balls, int(len(balls) * (percentage / 100)))
        if not to_transfer:
            await ctx.send(f"Percentage results in 0 {settings.plural_collectible_name} to transfer.", ephemeral=True)
            return

        if users:
            new_players = [(await Player.objects.aget_or_create(discord_id=uid))[0] for uid in pool_user_ids]
        else:
            assert user_2 is not None
            player_2, _ = await Player.objects.aget_or_create(discord_id=user_2.id)
            new_players = [player_2]

        rot = cycle(new_players)

        for ball in to_transfer:
            if clean_balls:
                await TradeObject.objects.filter(ballinstance_id=ball.pk).adelete()
                ball.trade_player_id = trade_player_id
                ball.spawned_time = None
                ball.catch_date = datetime.now()
                ball.server_id = None
                ball.favorite = False

            ball.player = next(rot)
            if users:
                ball.favorite = False
            await ball.asave()

        count = len(to_transfer)
        special_text = f" {special.name}" if special else ""
        target = f"pool of {len(pool_user_ids)} users" if users else str(user_2)
        await ctx.send(
            f"Transferred {count}{special_text} {settings.plural_collectible_name}"
            f" ({percentage}%) from {user_1} to {target}.",
            ephemeral=True,
        )
        target_log = f"pool: {', '.join(str(uid) for uid in pool_user_ids)}" if users else str(user_2)
        log.info(
            f"{ctx.author} transferred {count}{special_text} {settings.plural_collectible_name}"
            f" ({percentage}%) from {user_1} to {target_log}.",
            extra={"webhook": True},
        )
    else:
        if not countryball_id:
            await ctx.send("Please provide ball IDs.", ephemeral=True)
            return
        ball_ids_str = [bid.strip() for bid in countryball_id.split(",")]
        ball_ids = []

        try:
            for bid in ball_ids_str:
                ball_ids.append(int(bid, 16))
        except ValueError:
            await ctx.send(
                f"Invalid {settings.collectible_name} ID format. Use hex IDs separated by commas.", ephemeral=True
            )
            return

        balls_to_transfer = []
        for bid in ball_ids:
            try:
                ball = await BallInstance.objects.prefetch_related("player").aget(id=bid)
                if ball.deleted:
                    await ctx.send(
                        f"The {settings.collectible_name} {hex(bid)[2:].upper()} is deleted and cannot be transferred.",
                        ephemeral=True,
                    )
                    return
                balls_to_transfer.append(ball)
            except BallInstance.DoesNotExist:
                await ctx.send(
                    f"The {settings.collectible_name} ID {hex(bid)[2:].upper()} does not exist.", ephemeral=True
                )
                return

        if users:
            new_players = [(await Player.objects.aget_or_create(discord_id=uid))[0] for uid in pool_user_ids]
        else:
            recipient, _ = await Player.objects.aget_or_create(discord_id=user_1.id)
            new_players = [recipient]

        rot = cycle(new_players)

        transferred = []
        for ball in balls_to_transfer:
            original_player = ball.player
            new_player = next(rot)

            if original_player.discord_id == new_player.discord_id:
                continue

            if clean_balls:
                await TradeObject.objects.filter(ballinstance_id=ball.id).adelete()
                ball.trade_player_id = trade_player_id
                ball.spawned_time = None
                ball.catch_date = datetime.now()
                ball.server_id = None
                ball.favorite = False

            ball.player = new_player
            if users:
                ball.favorite = False
            await ball.asave()
            transferred.append((ball, original_player, new_player))

        if not transferred:
            target = f"pool of {len(pool_user_ids)} users" if users else str(user_1)
            await ctx.send(
                f"All specified {settings.plural_collectible_name} already belong to {target}.", ephemeral=True
            )
            return

        count = len(transferred)
        target = f"pool of {len(pool_user_ids)} users" if users else str(user_1)

        if count == 1 and not users:
            ball, original_player, new_player = transferred[0]
            await ctx.send(f"Transferred {ball}({ball.pk:0X}) from {original_player} to {new_player}.", ephemeral=True)
            log.info(
                f"{ctx.author} transferred {ball}({ball.pk:0X}) from {original_player} to {new_player}.",
                extra={"webhook": True},
            )
        else:
            ball_list = ", ".join([f"{b.pk:0X}" for b, _, _ in transferred])
            await ctx.send(f"Transferred {count} {settings.plural_collectible_name} to {target}.", ephemeral=True)
            target_log = f"pool: {', '.join(str(uid) for uid in pool_user_ids)}" if users else str(user_1)
            log.info(
                f"{ctx.author} transferred {count} {settings.plural_collectible_name} ({ball_list}) to {target_log}.",
                extra={"webhook": True},
            )


@balls.command(name="reset")
@checks.has_permissions("bd_models.delete_ballinstance", "bd_models.change_ballinstance")
async def balls_reset(
    ctx: commands.Context[BallsDexBot], user: discord.User, percentage: int | None = None, soft_delete: bool = True
):
    """
    Reset a player's countryballs.

    Parameters
    ----------
    user: discord.User
        The user you want to reset the countryballs of.
    percentage: int | None
        The percentage of countryballs to delete, if not all. Used for sanctions.
    soft_delete: bool
        If true, the countryballs will be marked as deleted instead of being removed from the
        database.
    """
    player = await Player.objects.aget_or_none(discord_id=user.id)
    if not player:
        await ctx.send("The user you gave does not exist.", ephemeral=True)
        return
    if percentage and not 0 < percentage < 100:
        await ctx.send("The percentage must be between 1 and 99.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)

    method = "soft" if soft_delete else "hard"
    if not percentage:
        text = f"Are you sure you want to {method} delete {user}'s {settings.plural_collectible_name}?"
    else:
        text = f"Are you sure you want to {method} delete {percentage}% of {user}'s {settings.plural_collectible_name}?"
    view = ConfirmChoiceView(
        ctx,
        accept_message=f"Confirmed, {method} deleting the {settings.plural_collectible_name}...",
        cancel_message="Request cancelled.",
    )
    await ctx.send(text, view=view, ephemeral=True)
    await view.wait()
    if not view.value:
        return
    if percentage:
        balls = [x async for x in BallInstance.objects.filter(player=player)]
        to_delete = random.sample(balls, int(len(balls) * (percentage / 100)))
        for ball in to_delete:
            if soft_delete:
                ball.deleted = True
                await ball.asave()
            else:
                await ball.adelete()
        count = len(to_delete)
    else:
        if soft_delete:
            count = await BallInstance.all_objects.filter(player=player).aupdate(deleted=True)
        else:
            count = await BallInstance.all_objects.filter(player=player).adelete()
    await ctx.send(f"{count} {settings.plural_collectible_name} from {user} have been deleted.", ephemeral=True)
    log.info(
        f"{ctx.author} deleted {percentage or 100}% of {player}'s {settings.plural_collectible_name}.",
        extra={"webhook": True},
    )


@balls.command(name="count")
@checks.has_permissions("bd_models.view_ballinstance")
async def balls_count(ctx: commands.Context[BallsDexBot], *, flags: BallsCountFlags):
    """
    Count the number of countryballs that a player has or how many exist in total.
    """
    filters = {}
    if flags.countryball:
        filters["ball"] = flags.countryball
    if flags.special:
        filters["special"] = flags.special
    if flags.user:
        filters["player__discord_id"] = flags.user.id
    await ctx.defer(ephemeral=True)
    qs = BallInstance.all_objects if flags.deleted else BallInstance.objects
    balls = await qs.filter(**filters).acount()
    verb = "is" if balls == 1 else "are"
    country = f"{flags.countryball.country} " if flags.countryball else ""
    plural = "s" if balls > 1 or balls == 0 else ""
    special_str = f"{flags.special.name} " if flags.special else ""
    if flags.user:
        await ctx.send(
            f"{flags.user} has {balls} {special_str}{country}{settings.collectible_name}{plural}.", ephemeral=True
        )
    else:
        await ctx.send(
            f"There {verb} {balls} {special_str}{country}{settings.collectible_name}{plural}.", ephemeral=True
        )


@balls.command()
@checks.has_permissions("bd_models.view_ball")
async def rarity(ctx: commands.Context["BallsDexBot"], *, flags: RarityFlags):
    """
    Generate a list of countryballs ranked by rarity.
    """
    text = ""
    balls_queryset = Ball.objects.all().order_by("rarity")
    if not flags.include_disabled:
        balls_queryset = balls_queryset.filter(rarity__gt=0, enabled=True)
    sorted_balls = [x async for x in balls_queryset]

    if flags.chunked:
        groups: dict[float, list[Ball]] = defaultdict(list)
        for ball in sorted_balls:
            groups[ball.rarity].append(ball)
        tier = 1
        lines = []
        for chunk in groups.values():
            lines.append(f"T{tier}:")
            for ball in chunk:
                lines.append(f"{ball.country}")
            lines.append("")
            tier += 1
        text = "\n".join(lines).rstrip()
    else:
        for i, ball in enumerate(sorted_balls, start=1):
            text += f"{i}. {ball.country}\n"

    view = discord.ui.LayoutView()
    text_display = discord.ui.TextDisplay("")
    view.add_item(text_display)
    menu = Menu(ctx.bot, view, TextSource(text, prefix="```md\n", suffix="```"), TextFormatter(text_display))
    await menu.init()
    await ctx.send(view=view, ephemeral=True)


@balls.command(name="farms")
@checks.has_permissions("bd_models.add_blacklistedguild")
async def balls_farms(ctx: commands.Context[BallsDexBot]):
    """
    Detect farm servers and optionally take action against them.
    """
    await ctx.defer(ephemeral=True)

    if not ctx.bot.user:
        await ctx.send("Bot user information unavailable.", ephemeral=True)
        return
    ESCROW_ID = ctx.bot.user.id
    escrow, created = await Player.objects.aget_or_create(discord_id=ESCROW_ID)

    qualifying_guilds = []

    for guild in ctx.bot.guilds:
        if not guild.member_count or guild.member_count < 15:
            try:
                config = await GuildConfig.objects.aget(guild_id=guild.id)
                if not config.enabled:
                    continue
            except GuildConfig.DoesNotExist:
                continue

            try:
                await BlacklistedGuild.objects.aget(discord_id=guild.id)
                continue
            except BlacklistedGuild.DoesNotExist:
                pass

            ball_count = await BallInstance.objects.filter(server_id=guild.id).acount()

            try:
                if guild.owner_id:
                    owner = await ctx.bot.fetch_user(guild.owner_id)
                    owner_name = f"{owner} ({owner.id})"
                else:
                    owner_name = "Unknown (no owner)"
            except Exception:
                owner_name = f"Unknown ({guild.owner_id})"

            qualifying_guilds.append(
                {
                    "id": guild.id,
                    "name": guild.name,
                    "owner": owner_name,
                    "member_count": guild.member_count,
                    "ball_count": ball_count,
                }
            )

    if not qualifying_guilds:
        await ctx.send("No farm servers found.", ephemeral=True)
        return

    owner_server_counts: dict[int, list[dict]] = {}
    for guild_data in qualifying_guilds:
        owner_id_str = guild_data["owner"].split("(")[-1].rstrip(")")
        try:
            owner_id = int(owner_id_str)
            if owner_id not in owner_server_counts:
                owner_server_counts[owner_id] = []
            owner_server_counts[owner_id].append(guild_data)
        except ValueError:
            pass

    fields = []
    for guild_data in qualifying_guilds:
        owner_id_str = guild_data["owner"].split("(")[-1].rstrip(")")
        try:
            owner_id = int(owner_id_str)
            if owner_id in owner_server_counts:
                server_num = owner_server_counts[owner_id].index(guild_data) + 1
                display_name = f"Server {server_num}"
            else:
                display_name = "Unknown Server"
        except ValueError:
            display_name = "Unknown Server"

        fields.append(
            {
                "name": f"{display_name} ({guild_data['id']})",
                "value": (
                    f"**Owner:** {guild_data['owner']}\n"
                    f"**Members:** {guild_data['member_count']}\n"
                    f"**{settings.plural_collectible_name.title()} Caught:** {guild_data['ball_count']}"
                ),
            }
        )

    embeds = []
    for i in range(0, len(fields), 5):
        embed = discord.Embed(
            title=f"Detected {len(qualifying_guilds)} Farm Server(s)",
            description="Click 'Punish!' to blacklist servers and transfer balls to escrow",
            color=discord.Color.orange(),
        )
        for field in fields[i : i + 5]:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        embed.set_footer(text=f"Page {len(embeds) + 1}/{(len(fields) + 4) // 5}")
        embeds.append(embed)

    class FarmPunishView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.page = 0
            self.punished = False
            self.message: discord.Message | None = None
            if len(embeds) <= 1:
                self.remove_item(self.prev_page)
                self.remove_item(self.next_page)
            else:
                self._update_buttons()

        def _update_buttons(self):
            self.prev_page.disabled = self.page == 0
            self.next_page.disabled = self.page >= len(embeds) - 1

        async def on_timeout(self):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.NotFound:
                    pass

        @discord.ui.button(label="Back", style=discord.ButtonStyle.blurple)
        async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("You cannot use this button.", ephemeral=True)
                return
            self.page = max(0, self.page - 1)
            self._update_buttons()
            await interaction.response.edit_message(embed=embeds[self.page], view=self)

        @discord.ui.button(label="Next", style=discord.ButtonStyle.blurple)
        async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("You cannot use this button.", ephemeral=True)
                return
            self.page = min(len(embeds) - 1, self.page + 1)
            self._update_buttons()
            await interaction.response.edit_message(embed=embeds[self.page], view=self)

        @discord.ui.button(label="Punish!", style=discord.ButtonStyle.danger)
        async def punish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("You cannot use this button.", ephemeral=True)
                return

            if self.punished:
                await interaction.response.send_message("Already punished these servers.", ephemeral=True)
                return

            self.punished = True
            button.disabled = True
            await interaction.response.edit_message(view=self)

            moderator_id = interaction.user.id
            reason = f"Server under 15 members.\nBy: {interaction.user} ({moderator_id})"

            total_transferred = 0

            for guild_data in qualifying_guilds:
                gid = guild_data["id"]

                async for ball in BallInstance.objects.filter(server_id=gid):
                    await TradeObject.objects.filter(ballinstance_id=ball.pk).adelete()
                    ball.player = escrow
                    ball.favorite = False
                    ball.server_id = None
                    ball.trade_player = escrow
                    ball.spawned_time = None
                    ball.catch_date = datetime.now()
                    await ball.asave()
                    total_transferred += 1

                await BlacklistedGuild.objects.acreate(discord_id=gid, reason=reason, moderator_id=moderator_id)
                await BlacklistHistory.objects.acreate(
                    discord_id=gid, reason=reason, moderator_id=moderator_id, id_type="guild"
                )
                ctx.bot.blacklist_guild.add(gid)
                log.info(
                    f"{interaction.user} blacklisted farm guild {guild_data['name']}({gid}) - "
                    f"Owner: {guild_data['owner']}, Members: {guild_data['member_count']}, "
                    f"{settings.plural_collectible_name}: {guild_data['ball_count']}",
                    extra={"webhook": True},
                )

            summary = (
                f"**Farm Detection Complete**\n\n"
                f"{'Server' if len(qualifying_guilds) == 1 else 'Servers'} Blacklisted: {len(qualifying_guilds)}\n"
                f"Total {settings.collectible_name if total_transferred == 1 else settings.plural_collectible_name}"
                f" Transferred: {total_transferred}"
            )
            await interaction.followup.send(summary, ephemeral=True)

    view = FarmPunishView()
    view.message = await ctx.send(embed=embeds[0], view=view)
