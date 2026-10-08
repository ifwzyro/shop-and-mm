"""Staff slash commands: overview, forceclaim, release, close."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from database.models import TicketStatus
from utils import permissions
from utils.checks import require_guild, require_staff, resolve_ticket_ref, send_interaction
from utils.emojis import with_emoji
from utils.formatting import (
    error_notice,
    status_label,
    success_notice,
    truncate,
)
from utils.time import now
from cogs.mm import mm_group

log = logging.getLogger("mm.cogs.staff")


def _member(interaction: discord.Interaction) -> Optional[discord.Member]:
    return interaction.member if isinstance(interaction.member, discord.Member) else None


@mm_group.command(name="staff", description="Staff overview: active tickets, flags and reports")
@require_staff()
async def mm_staff(interaction: discord.Interaction) -> None:
    db = interaction.client.db
    active = await db.list_active_tickets()
    flagged = [t for t in active if t.flagged]
    reports = await db.open_report_count()

    counts = await db.status_counts()
    stage_lines = []
    for status in (
        TicketStatus.WAITING_FOR_PARTNER,
        TicketStatus.WAITING_FOR_ROLES,
        TicketStatus.ROLES_SELECTED,
        TicketStatus.WAITING_FOR_CONFIRMATION,
        TicketStatus.WAITING_FOR_MM,
        TicketStatus.MM_CLAIMED,
        TicketStatus.IN_PROGRESS,
        TicketStatus.DISPUTED,
    ):
        if counts.get(status):
            stage_lines.append(f"{status_label(status)}: {counts[status]}")

    from views.base import PanelData, plain_panel_view

    fields: list[tuple[str, str]] = [
        ("Active tickets", str(len(active))),
        ("Open reports", str(reports)),
        ("By stage", "\n".join(stage_lines) if stage_lines else "None"),
    ]
    if flagged:
        lines = []
        for ticket in flagged[:10]:
            link = f"<#{ticket.channel_id}>" if ticket.channel_id else "channel missing"
            lines.append(f"**{ticket.label}** — {status_label(ticket.status)} — {link}")
        fields.append(
            (f"Flagged for review ({len(flagged)})", truncate("\n".join(lines), 1000))
        )

    await send_interaction(
        interaction,
        view=plain_panel_view(
            PanelData(
                title=with_emoji("staff", "MM Staff Overview"),
                body="Current state of the escrow system.",
                fields=fields,
            )
        ),
        ephemeral=True,
    )


@mm_group.command(name="forceclaim", description="Force-claim an unclaimed ticket (staff recovery)")
@require_staff()
async def mm_forceclaim(interaction: discord.Interaction, ticket: Optional[str] = None) -> None:
    db = interaction.client.db
    assert interaction.guild is not None
    member = _member(interaction)
    if member is None:
        await send_interaction(interaction, content="Server only.", ephemeral=True)
        return

    resolved = await resolve_ticket_ref(
        db, interaction.guild, ticket, fallback_channel_id=interaction.channel_id
    )
    if resolved is None:
        await send_interaction(
            interaction,
            view=error_notice("Ticket not found", "Usage: `/mm forceclaim ticket:MM-0042`."),
            ephemeral=True,
        )
        return
    if not resolved.is_active:
        await send_interaction(
            interaction,
            view=error_notice("Closed", f"**{resolved.label}** is not active."),
            ephemeral=True,
        )
        return
    if resolved.claimed_mm_id is not None:
        await send_interaction(
            interaction,
            view=error_notice(
                "Already claimed",
                f"**{resolved.label}** is claimed by <@{resolved.claimed_mm_id}>. "
                "Use `/mm release` first.",
            ),
            ephemeral=True,
        )
        return

    result = await db.force_claim_ticket(resolved.id, member.id, member.id, now())
    if not result.ok:
        message = (
            f"This ticket was just claimed by <@{result.claimed_by}>."
            if result.claimed_by
            else "This ticket can't be claimed right now."
        )
        await send_interaction(interaction, view=error_notice("Could not claim", message), ephemeral=True)
        return

    fresh = await db.get_ticket(resolved.id) or resolved
    from views.mm_panel import apply_claimed_status

    fresh = await apply_claimed_status(db, fresh)
    await _render_assigned_panel(interaction, fresh, member)
    await send_interaction(
        interaction,
        view=success_notice(
            "Force claim successful",
            f"You are now the middleman for **{fresh.label}** (staff override).",
        ),
        ephemeral=True,
    )
    log.info("Staff force claim of %s by %s", fresh.label, member.id)


async def _render_assigned_panel(
    interaction: discord.Interaction, ticket, member: discord.Member
) -> None:
    """Send or update the assigned-MM panel after a staff force claim."""
    from views.base import build_assigned_data, edit_panel, send_and_store
    from views.mm_panel import AssignedView

    guild = interaction.guild
    if guild is None:
        return
    data = build_assigned_data(ticket, member.id, member.display_avatar.url)
    if not await edit_panel(
        guild,
        ticket,
        "mm_message_id",
        content=None,
        build=lambda: AssignedView(ticket.id, stage="full", data=data),
    ):
        channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
        if channel is not None:
            await send_and_store(
                interaction.client.db,
                channel,
                ticket,
                "mm_message_id",
                view=AssignedView(ticket.id, stage="full", data=data),
            )


@mm_group.command(name="release", description="Release the assigned MM and reopen the claim (staff/MM)")
@require_guild()
async def mm_release(interaction: discord.Interaction, ticket: Optional[str] = None) -> None:
    db = interaction.client.db
    assert interaction.guild is not None
    member = _member(interaction)

    resolved = await resolve_ticket_ref(
        db, interaction.guild, ticket, fallback_channel_id=interaction.channel_id
    )
    if resolved is None:
        await send_interaction(
            interaction,
            view=error_notice("Ticket not found", "Usage: `/mm release ticket:MM-0042`."),
            ephemeral=True,
        )
        return
    is_assigned = resolved.claimed_mm_id == interaction.user.id
    if not (permissions.is_staff(member) or is_assigned):
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Only staff or the assigned middleman can release."),
            ephemeral=True,
        )
        return
    if resolved.claimed_mm_id is None:
        await send_interaction(
            interaction,
            view=error_notice("No middleman", f"**{resolved.label}** has no assigned middleman."),
            ephemeral=True,
        )
        return

    if not await db.release_ticket(resolved.id, interaction.user.id, now(), reason="released via /mm release"):
        await send_interaction(
            interaction,
            view=error_notice("Could not release", "The ticket state changed — please retry."),
            ephemeral=True,
        )
        return

    fresh = await db.get_ticket(resolved.id) or resolved
    guild = interaction.guild
    from views.base import build_mm_request_data, edit_panel, refresh_status_panel
    from views.mm_panel import ClaimView, mm_request_ping

    content, allowed = mm_request_ping(guild)
    updated = await edit_panel(
        guild,
        fresh,
        "mm_message_id",
        content=content,
        build=lambda: ClaimView(fresh.id, data=build_mm_request_data(fresh)),
        allowed_mentions=allowed,
    )
    if not updated and fresh.channel_id:
        channel = guild.get_channel(fresh.channel_id)
        if channel is not None:
            from views.base import send_and_store

            await send_and_store(
                db, channel, fresh, "mm_message_id",
                content=content,
                view=ClaimView(fresh.id, data=build_mm_request_data(fresh)),
                allowed_mentions=allowed,
            )
    await refresh_status_panel(interaction.client, fresh)

    await send_interaction(
        interaction,
        view=success_notice(
            "Released",
            f"**{fresh.label}** is open again — the MM role has been pinged.",
        ),
        ephemeral=True,
    )
    log.info("MM released on %s by %s via command", fresh.label, interaction.user.id)


@mm_group.command(name="close", description="Force-close a ticket and preserve its audit history (staff)")
@require_staff()
async def mm_close(
    interaction: discord.Interaction,
    ticket: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    db = interaction.client.db
    assert interaction.guild is not None

    resolved = await resolve_ticket_ref(
        db, interaction.guild, ticket, fallback_channel_id=interaction.channel_id
    )
    if resolved is None:
        await send_interaction(
            interaction,
            view=error_notice("Ticket not found", "Usage: `/mm close ticket:MM-0042 reason:...`."),
            ephemeral=True,
        )
        return
    if resolved.status == TicketStatus.CLOSED:
        await send_interaction(
            interaction,
            view=error_notice("Already closed", f"**{resolved.label}** is already closed."),
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        return

    if not await db.close_ticket(resolved.id, interaction.user.id, now(), reason):
        await interaction.followup.send(
            view=error_notice("Could not close", "The ticket was already closed."), ephemeral=True
        )
        return

    fresh = await db.get_ticket(resolved.id) or resolved
    guild = interaction.guild
    if guild is not None:
        from views.base import build_terminal_data, edit_panel, plain_panel_view

        terminal = build_terminal_data(fresh)
        for field_name in ("status_message_id", "confirm_message_id"):
            await edit_panel(
                guild,
                fresh,
                field_name,
                build=lambda data=terminal: plain_panel_view(data),
            )
        await edit_panel(
            guild,
            fresh,
            "mm_message_id",
            build=lambda: _finished_view(fresh.id, terminal),
        )

    await interaction.followup.send(
        view=success_notice("Ticket closed", f"**{fresh.label}** has been closed."),
        ephemeral=True,
    )
    log.info("Ticket %s closed by %s (reason=%s)", fresh.label, interaction.user.id, reason)

    from views.base import generate_ticket_transcript

    await generate_ticket_transcript(interaction.client, fresh)


def _finished_view(ticket_id: int, data=None):
    from views.mm_panel import AssignedView

    return AssignedView(ticket_id, stage="finished", data=data)


async def setup(bot: commands.Bot) -> None:
    # Commands register onto the shared /mm group at import time.
    log.debug("staff cog loaded")
