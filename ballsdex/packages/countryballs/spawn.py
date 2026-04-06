import asyncio
import logging
import random
from abc import abstractmethod
from collections import deque, namedtuple
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import discord
from discord.utils import format_dt

from settings.models import settings

if TYPE_CHECKING:
    from discord.ext.commands import Context

    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.countryballs")

CachedMessage = namedtuple("CachedMessage", ["content", "author_id"])


class BaseSpawnManager:
    """
    A class instancied on cog load that will include the logic determining when a countryball
    should be spawned. You can implement your own version and configure it in config.yml.

    Be careful with optimization and memory footprint, this will be called very often and should
    not slow down the bot or cause memory leaks.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @abstractmethod
    async def handle_message(self, message: discord.Message) -> bool | tuple[Literal[True], str]:
        """
        Handle a message event and determine if a countryball should be spawned next.

        Parameters
        ----------
        message: discord.Message
            The message that triggered the event

        Returns
        -------
        bool | tuple[Literal[True], str]
            `True` if a countryball should be spawned, else `False`.

            If a countryball should spawn, do not forget to cleanup induced context to avoid
            infinite spawns.

            You can also return a tuple (True, msg) to indicate which spawn algorithm has been
            used, which is then reported to prometheus. This is useful for comparing the results
            of your algorithms using A/B testing.
        """
        raise NotImplementedError

    @abstractmethod
    async def admin_explain(self, ctx: "Context[BallsDexBot]", guild: discord.Guild):
        """
        Invoked by "/admin cooldown", this function should provide insights of the cooldown
        system for admins.

        Parameters
        ----------
        ctx: ~discord.ext.commands.Context[BallsDexBot]
            The context of the invoking hybrid command
        guild: discord.Guild
            The guild that is targeted for the insights
        """
        raise NotImplementedError


@dataclass
class SpawnCooldown:
    """
    Represents the default spawn internal system per guild. Contains the counters that will
    determine if a countryball should be spawned next or not.

    Attributes
    ----------
    time: datetime
        Time when the object was initialized. Block spawning when it's been less than ten minutes
    scaled_message_count: float
        A number starting at 0, incrementing with the messages until reaching `threshold`. At this
        point, a ball will be spawned next.
    threshold: int
        The number `scaled_message_count` has to reach for spawn.
        Determined randomly with `SPAWN_CHANCE_RANGE`
    lock: asyncio.Lock
        Used to ratelimit messages and ignore fast spam
    message_cache: ~collections.deque[CachedMessage]
        A list of recent messages used to reduce the spawn chance when too few different chatters
        are present. Limited to the 100 most recent messages in the guild.
    """

    time: datetime
    # initialize partially started, to reduce the dead time after starting the bot
    scaled_message_count: float = field(default_factory=lambda: settings.spawn_chance_min // 2)
    threshold: int = field(default_factory=lambda: random.randint(settings.spawn_chance_min, settings.spawn_chance_max))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    message_cache: deque[CachedMessage] = field(default_factory=lambda: deque(maxlen=100))

    def reset(self, time: datetime):
        self.scaled_message_count = 1.0
        self.threshold = random.randint(settings.spawn_chance_min, settings.spawn_chance_max)
        try:
            self.lock.release()
        except RuntimeError:  # lock is not acquired
            pass
        self.time = time

    async def increase(self, message: discord.Message) -> bool:
        self.message_cache.append(CachedMessage(content=message.content, author_id=message.author.id))

        if self.lock.locked():
            return False

        async with self.lock:
            message_multiplier = 1

            # Penalty uses total member_count (fast and sufficient for penalty)
            member_count = message.guild.member_count or 0
            if member_count < 15 or member_count > 1500:
                message_multiplier /= 2

            if message._state.intents.message_content and len(message.content) < 5:
                message_multiplier /= 2

            if len(set(x.author_id for x in self.message_cache)) < 4 or (
                len(list(filter(lambda x: x.author_id == message.author.id, self.message_cache)))
                / self.message_cache.maxlen
                > 0.4
            ):
                message_multiplier /= 2

            self.scaled_message_count += message_multiplier
            await asyncio.sleep(10)

        return True


class SpawnManager(BaseSpawnManager):
    def __init__(self, bot: "BallsDexBot"):
        super().__init__(bot)
        self.cooldowns: dict[int, SpawnCooldown] = {}

    async def handle_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if not guild:
            return False

        if self.bot.intents.members:
            human_count = sum(1 for member in guild.members if not member.bot)
        else:
            human_count = guild.member_count or 0

        if human_count < 15:
            return False

        cooldown = self.cooldowns.get(guild.id, None)
        if not cooldown:
            cooldown = SpawnCooldown(message.created_at)
            self.cooldowns[guild.id] = cooldown

        delta_t = (message.created_at - cooldown.time).total_seconds()

        # Time multiplier based on server size
        if human_count < 15:
            time_multiplier = 0.1
        elif human_count < 150:
            time_multiplier = 0.8
        elif human_count < 1500:
            time_multiplier = 0.5
        else:
            time_multiplier = 0.2

        if not await cooldown.increase(message):
            return False

        if cooldown.scaled_message_count + time_multiplier * (delta_t // 60) <= cooldown.threshold:
            return False

        if delta_t < 600:
            return False

        cooldown.reset(message.created_at)
        return True

    async def admin_explain(self, ctx: "Context[BallsDexBot]", guild: discord.Guild):
        cooldown = self.cooldowns.get(guild.id)
        if not cooldown:
            await ctx.send(
                "No spawn manager could be found for that guild. Spawn may have been disabled.", 
                ephemeral=True
            )
            return

        if not guild.member_count:
            await ctx.send("`member_count` data not returned for this guild, spawn cannot work.")
            return

        embed = discord.Embed()
        embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        embed.colour = discord.Colour.orange()

        delta = (
            (ctx.interaction.created_at if ctx.interaction else ctx.message.created_at) 
            - cooldown.time
        ).total_seconds()

        # Time multiplier based on server size
        if guild.member_count < 15:
            multiplier = 0.1
            range_str = "1-14"
        elif guild.member_count < 150:
            multiplier = 0.8
            range_str = "15-149"
        elif guild.member_count < 1500:
            multiplier = 0.5
            range_str = "150-1499"
        else:
            multiplier = 0.2
            range_str = "1500+"

        penalities: list[str] = []
        if guild.member_count < 15 or guild.member_count > 1500:
            penalities.append("Server has less than 15 or more than 1500 members")

        if any(len(x.content) < 5 for x in cooldown.message_cache):
            penalities.append("Some cached messages are less than 5 characters long")

        authors_set = set(x.author_id for x in cooldown.message_cache)
        low_chatters = len(authors_set) < 4

        # Check if one author has more than 40% of messages in cache
        major_chatter = any(
            len(list(filter(lambda x: x.author_id == author, cooldown.message_cache)))
            / cooldown.message_cache.maxlen > 0.4
            for author in authors_set
        )

        # Build penalties
        if low_chatters:
            if not major_chatter:
                penalities.append("Message cache has less than 4 chatters")
            else:
                penalities.append(
                    "Message cache has less than 4 chatters **and** "
                    "one user has more than 40% of messages within message cache"
                )
        elif major_chatter:
            if not low_chatters:
                penalities.append("One user has more than 40% of messages within cache")

        penality_multiplier = 0.5 ** len(penalities)

        if penalities:
            embed.add_field(
                name="\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Penalities",
                value="Each penality divides the progress by 2\n\n- " + "\n- ".join(penalities),
            )

        chance = cooldown.threshold - multiplier * (delta // 60)

        embed.description = (
            f"Manager initiated **{format_dt(cooldown.time, style='R')}**\n"
            f"Initial number of points to reach: **{cooldown.threshold}**\n"
            f"Message cache length: **{len(cooldown.message_cache)}**\n\n"
            f"Time-based multiplier: **x{multiplier}** *({range_str} members)*\n"
            "*This affects how much the number of points to reach reduces over time*\n"
            f"Penality multiplier: **x{penality_multiplier}**\n"
            "*This affects how much a message sent increases the number of points*\n\n"
            f"__Current count: **{cooldown.threshold}/{chance}**__\n\n"
        )

        informations: list[str] = []
        if cooldown.lock.locked():
            informations.append("The manager is currently on cooldown.")
        if delta < 600:
            informations.append(
                f"The manager is less than 10 minutes old, {settings.plural_collectible_name} "
                "cannot spawn at the moment."
            )

        if informations:
            embed.add_field(
                name="\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16} Informations",
                value="- " + "\n- ".join(informations),
            )

        await ctx.send(embed=embed, ephemeral=True)
