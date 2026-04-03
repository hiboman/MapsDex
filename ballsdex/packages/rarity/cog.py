from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, TextDisplay

from ballsdex.core.discord import LayoutView
from bd_models.models import Ball
from bd_models.models import balls as all_balls_cache
from settings.models import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.rarity")

# Tiers ordered rarest → most common.
# threshold = minimum rarity percentile to qualify (1.0 = rarest end, 0.0 = most common end).
TIERS: list[tuple[str, str, discord.Color, float]] = [
    ("Legendary", "🔴", discord.Color.gold(), 0.90),
    ("Epic", "🟠", discord.Color.purple(), 0.75),
    ("Rare", "🟣", discord.Color.blue(), 0.50),
    ("Uncommon", "🔵", discord.Color.green(), 0.25),
    ("Common", "🟢", discord.Color.light_grey(), 0.00),
]

TIER_NAMES = [t[0] for t in TIERS]
BALLS_PER_PAGE = 15


def get_tier(percentile: float) -> tuple[str, str, discord.Color]:
    """Return (name, emoji, color) for a rarity percentile (1.0 = rarest, 0.0 = most common)."""
    for name, emoji, color, threshold in TIERS:
        if percentile >= threshold:
            return name, emoji, color
    return TIERS[-1][0], TIERS[-1][1], TIERS[-1][2]


def compute_ball_tiers(enabled_balls: list[Ball]) -> list[tuple[Ball, str, str, discord.Color, float]]:
    """
    Compute rarity tiers for a list of enabled balls.

    Higher rarity weight = more common. Returns a list sorted rarest-first (ascending weight),
    each entry being (ball, tier_name, tier_emoji, tier_color, spawn_probability_pct).
    """
    if not enabled_balls:
        return []

    total_weight = sum(b.rarity for b in enabled_balls)
    sorted_balls = sorted(enabled_balls, key=lambda b: b.rarity)  # ascending = rarest first
    n = len(sorted_balls)

    result = []
    for rank, ball in enumerate(sorted_balls):
        percentile = 1.0 - (rank / n)  # rank 0 → percentile 1.0 (rarest)
        tier_name, tier_emoji, tier_color = get_tier(percentile)
        prob = (ball.rarity / total_weight * 100) if total_weight > 0 else 0.0
        result.append((ball, tier_name, tier_emoji, tier_color, prob))

    return result


class RarityListView(LayoutView):
    """Paginated LayoutView for /rarity list."""

    controls = ActionRow()

    def __init__(self, pages: list[str], author_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.current = 0
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        self.add_item(TextDisplay(self.pages[self.current]))
        self.add_item(TextDisplay(f"-# Page {self.current + 1}/{len(self.pages)}"))
        self.prev_button.disabled = self.current == 0
        self.next_button.disabled = self.current >= len(self.pages) - 1
        self.add_item(self.controls)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True

    @controls.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        self.current -= 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    @controls.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        self.current += 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        self.prev_button.disabled = True
        self.next_button.disabled = True
        self.clear_items()
        self.add_item(TextDisplay(self.pages[self.current]))
        self.add_item(TextDisplay(f"-# Page {self.current + 1}/{len(self.pages)} · Session expired"))
        self.add_item(self.controls)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _build_list_pages(entries: list[tuple[Ball, str, str, discord.Color, float]], filter_tier: str | None) -> list[str]:
    if filter_tier:
        entries = [e for e in entries if e[1].lower() == filter_tier.lower()]
    if not entries:
        return []

    chunks = [entries[i : i + BALLS_PER_PAGE] for i in range(0, len(entries), BALLS_PER_PAGE)]
    pages: list[str] = []

    for page_num, chunk in enumerate(chunks, start=1):
        header = (
            f"## {settings.plural_collectible_name.capitalize()} by Rarity"
            + (f" — {filter_tier}" if filter_tier else "")
            + "\n"
            f"-# Sorted rarest → most common. "
            f"Spawn % = relative weight among all enabled {settings.plural_collectible_name}.\n\n"
        )
        lines = [
            f"{tier_emoji} **{ball.country}** — {tier_name} `({prob:.3f}%)`"
            for ball, tier_name, tier_emoji, _, prob in chunk
        ]
        pages.append(header + "\n".join(lines))

    return pages


class Rarity(commands.GroupCog, name="rarity"):
    """Commands to explore the rarity of {plural_collectible_name}."""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    def _get_enabled_balls(self) -> list[Ball]:
        return [b for b in all_balls_cache.values() if b.enabled]

    @app_commands.command()
    async def tiers(self, interaction: discord.Interaction["BallsDexBot"]):
        """Show all rarity tiers, how many collectibles each contains, and their spawn chance range."""
        await interaction.response.defer(thinking=True)

        enabled = self._get_enabled_balls()
        if not enabled:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} are currently enabled.", ephemeral=True
            )
            return

        entries = compute_ball_tiers(enabled)
        total = len(entries)

        tier_data: dict[str, list[float]] = {name: [] for name, *_ in TIERS}
        for _, tier_name, _, _, prob in entries:
            tier_data[tier_name].append(prob)

        lines = [
            f"## {settings.plural_collectible_name.capitalize()} — Rarity Tiers",
            f"-# {total} enabled {settings.plural_collectible_name} across {len(TIERS)} tiers.\n",
        ]

        percentile_labels = {
            "Legendary": "Bottom 10%",
            "Epic": "10–25%",
            "Rare": "25–50%",
            "Uncommon": "50–75%",
            "Common": "Top 25%",
        }

        for tier_name, tier_emoji, _, _ in TIERS:
            probs = tier_data[tier_name]
            count = len(probs)
            if count == 0:
                prob_range = "—"
            elif count == 1:
                prob_range = f"`{probs[0]:.3f}%`"
            else:
                prob_range = f"`{min(probs):.3f}% – {max(probs):.3f}%`"

            lines.append(
                f"### {tier_emoji} {tier_name}\n"
                f"**{count}** {settings.plural_collectible_name} · "
                f"Spawn chance: {prob_range} · "
                f"Rank: {percentile_labels.get(tier_name, '')}"
            )

        lines.append(f"\n-# Use `/rarity list` to browse all {settings.plural_collectible_name} by tier.")

        view = LayoutView()
        view.add_item(TextDisplay("\n".join(lines)))
        await interaction.followup.send(view=view)

    @app_commands.command()
    @app_commands.describe(tier="Filter by rarity tier (leave empty to show all)")
    @app_commands.choices(tier=[app_commands.Choice(name=name, value=name) for name in TIER_NAMES])
    async def list(self, interaction: discord.Interaction["BallsDexBot"], tier: app_commands.Choice[str] | None = None):
        """List all collectibles sorted from rarest to most common."""
        await interaction.response.defer(thinking=True)

        enabled = self._get_enabled_balls()
        if not enabled:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} are currently enabled.", ephemeral=True
            )
            return

        entries = compute_ball_tiers(enabled)
        filter_tier = tier.value if tier else None
        pages = _build_list_pages(entries, filter_tier)

        if not pages:
            label = f"**{filter_tier}**" if filter_tier else ""
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} found" + (f" in the {label} tier." if label else "."),
                ephemeral=True,
            )
            return

        view = RarityListView(pages, interaction.user.id)
        await interaction.followup.send(view=view)
        view.message = await interaction.original_response()

    @app_commands.command()
    @app_commands.describe(name="Name of the collectible to look up")
    async def info(self, interaction: discord.Interaction["BallsDexBot"], name: str):
        """Check the rarity tier and spawn probability of a specific collectible."""
        await interaction.response.defer(thinking=True)

        enabled = self._get_enabled_balls()
        if not enabled:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} are currently enabled.", ephemeral=True
            )
            return

        needle = name.strip().lower()
        match: Ball | None = None
        for ball in enabled:
            if ball.country.lower() == needle:
                match = ball
                break
        if match is None:
            for ball in enabled:
                if needle in ball.country.lower():
                    match = ball
                    break

        if match is None:
            await interaction.followup.send(
                f"No enabled {settings.collectible_name} named **{name}** was found.", ephemeral=True
            )
            return

        entries = compute_ball_tiers(enabled)
        ball_entry = next((e for e in entries if e[0].pk == match.pk), None)
        if ball_entry is None:
            await interaction.followup.send("Could not compute rarity data.", ephemeral=True)
            return

        ball, tier_name, tier_emoji, _, prob = ball_entry
        rank = next(i + 1 for i, e in enumerate(entries) if e[0].pk == ball.pk)
        total = len(entries)

        content = (
            f"**Tier:** {tier_emoji} {tier_name}\n"
            f"**Spawn Probability:** `{prob:.3f}%`\n"
            f"**Rarity Rank:** `#{rank}` out of {total} {settings.plural_collectible_name}\n"
            f"**Rarity Weight:** `{ball.rarity}`\n"
            f"-# Use /rarity list to browse all {settings.plural_collectible_name}."
        )
        view = LayoutView()
        view.add_item(TextDisplay(content))
        await interaction.followup.send(view=view)

    @info.autocomplete("name")
    async def info_autocomplete(
        self, interaction: discord.Interaction["BallsDexBot"], current: str
    ) -> list[app_commands.Choice[str]]:
        enabled = self._get_enabled_balls()
        needle = current.strip().lower()
        matches = sorted((b for b in enabled if needle in b.country.lower()), key=lambda b: b.country.lower())
        return [app_commands.Choice(name=b.country, value=b.country) for b in matches[:25]]
