"""Report slash command: /mm reportinfo."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from utils.checks import require_staff, send_interaction
from utils.formatting import error_notice
from cogs.mm import mm_group

log = logging.getLogger("mm.cogs.reports")


@mm_group.command(name="reportinfo", description="Show a full MM report by its ID (staff)")
@require_staff()
async def mm_reportinfo(interaction: discord.Interaction, report_id: int) -> None:
    db = interaction.client.db
    report = await db.get_report(report_id)
    if report is None:
        await send_interaction(
            interaction,
            view=error_notice("Not found", f"No report with ID `{report_id}` exists."),
            ephemeral=True,
        )
        return
    ticket = await db.get_ticket(report.ticket_id)
    if ticket is None:
        await send_interaction(
            interaction,
            view=error_notice("Not found", "The ticket related to this report no longer exists."),
            ephemeral=True,
        )
        return

    from views.base import plain_panel_view
    from views.report_views import build_report_data

    try:
        data = await build_report_data(interaction.client, report, ticket)
    except Exception:
        log.exception("Could not render report #%s", report.id)
        await send_interaction(
            interaction,
            view=error_notice("Render failed", "This report could not be rendered right now."),
            ephemeral=True,
        )
        return
    await send_interaction(interaction, view=plain_panel_view(data), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    # Commands register onto the shared /mm group at import time.
    log.debug("reports cog loaded")
