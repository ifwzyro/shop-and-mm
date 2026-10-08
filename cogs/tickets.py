"""Ticket slash commands: /mm ticket, /mm ticketinfo."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import require_guild, resolve_ticket_ref, send_interaction
from utils.emojis import with_emoji
from utils.formatting import error_notice, format_timeline, link_notice, notice, truncate
from views.base import plain_panel_view
from cogs.mm import build_ticket_overview, mm_group

log = logging.getLogger("mm.cogs.tickets")


@mm_group.command(name="ticket", description="Open a link to your active MM ticket")
@require_guild()
async def mm_ticket(interaction: discord.Interaction) -> None:
    db = interaction.client.db
    ticket = await db.find_active_ticket_for_user(interaction.user.id)
    if ticket is None and interaction.channel_id:
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
    if ticket is None:
        await send_interaction(
            interaction,
            view=notice(
                "No active ticket",
                "You don't have an active MM ticket.\n"
                "Use **Create MM Ticket** on the panel to open one.",
            ),
            ephemeral=True,
        )
        return

    status_text = f"Status: **{ticket.status.replace('_', ' ').title()}**"
    if ticket.channel_id:
        url = f"https://discord.com/channels/{interaction.guild_id}/{ticket.channel_id}"
        await send_interaction(
            interaction,
            view=link_notice(
                f"**{ticket.label}**\n{status_text}",
                f"Open {ticket.label}",
                url,
            ),
            ephemeral=True,
        )
    else:
        await send_interaction(
            interaction,
            view=notice(with_emoji("ticket", ticket.label), status_text),
            ephemeral=True,
        )


@mm_group.command(name="ticketinfo", description="Show ticket details and its audit timeline")
@require_guild()
async def mm_ticketinfo(interaction: discord.Interaction, ticket: Optional[str] = None) -> None:
    db = interaction.client.db
    assert interaction.guild is not None

    resolved = await resolve_ticket_ref(
        db, interaction.guild, ticket, fallback_channel_id=interaction.channel_id
    )
    if resolved is None:
        await send_interaction(
            interaction,
            view=error_notice(
                "Ticket not found",
                "Usage: `/mm ticketinfo ticket:MM-0042` (or run it inside a ticket channel).",
            ),
            ephemeral=True,
        )
        return

    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    is_insider = interaction.user.id in (resolved.creator_id, resolved.partner_id) or (
        resolved.claimed_mm_id == interaction.user.id
    )
    from utils import permissions

    if not (permissions.is_staff(member) or is_insider):
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Only staff or participants of this ticket can view it."),
            ephemeral=True,
        )
        return

    traders = await db.get_traders(resolved.id)
    data = build_ticket_overview(resolved, traders)

    events = await db.get_events(resolved.id, limit=8)
    if events:
        data.fields.append(
            ("Recent audit events", truncate(format_timeline(events, resolved.label), 1000))
        )
    await send_interaction(
        interaction, view=plain_panel_view(data), ephemeral=True
    )


async def setup(bot: commands.Bot) -> None:
    # Commands register onto the shared /mm group at import time.
    log.debug("tickets cog loaded")
