"""Staff moderation views: report acknowledge / resolve / escalate buttons."""

from __future__ import annotations

import logging
from typing import Optional

import discord

import config
from database.models import Report, ReportStatus, Ticket, TicketStatus
from utils import permissions
from utils.checks import send_interaction
from utils.emojis import button_emoji
from utils.formatting import error_notice
from utils.time import now
from views.base import BaseView, PanelData, panel_container, panel_footer

log = logging.getLogger("mm.views.staff")

_VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "ack": (ReportStatus.OPEN, ReportStatus.DELIVERY_FAILED, ReportStatus.ESCALATED),
    "resolve": (
        ReportStatus.OPEN,
        ReportStatus.ACKNOWLEDGED,
        ReportStatus.ESCALATED,
        ReportStatus.DELIVERY_FAILED,
    ),
    "escalate": (ReportStatus.OPEN, ReportStatus.ACKNOWLEDGED),
}


class ReportModView(BaseView):
    """Moderator action buttons attached to the report embed."""

    def __init__(
        self, report_id: int, resolved: bool = False, *, data: Optional[PanelData] = None
    ):
        super().__init__(ticket_id=None)
        self.report_id = report_id
        data = data or PanelData("MM REPORT")
        container = panel_container(data)

        ack = discord.ui.Button(
            label="Acknowledge",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm:rep_ack:{report_id}",
            emoji=button_emoji("confirm"),
            disabled=resolved,
        )
        ack.callback = self._on_ack
        container.add_item(ack)

        resolve = discord.ui.Button(
            label="Resolve",
            style=discord.ButtonStyle.success,
            custom_id=f"mm:rep_res:{report_id}",
            emoji=button_emoji("success"),
            disabled=resolved,
        )
        resolve.callback = self._on_resolve
        container.add_item(resolve)

        escalate = discord.ui.Button(
            label="Escalate",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm:rep_esc:{report_id}",
            emoji=button_emoji("warning"),
            disabled=resolved,
        )
        escalate.callback = self._on_escalate
        container.add_item(escalate)
        panel_footer(container, data)
        self.add_item(container)

    # -- guards ---------------------------------------------------------

    async def _load(self, interaction: discord.Interaction) -> Optional[tuple[Report, Ticket]]:
        member = interaction.member if isinstance(interaction.member, discord.Member) else None
        if not permissions.can_moderate_reports(member):
            await send_interaction(
                interaction,
                view=error_notice("Not allowed", "Only staff can manage reports."),
                ephemeral=True,
            )
            return None
        db = interaction.client.db
        report = await db.get_report(self.report_id)
        if report is None:
            await send_interaction(
                interaction,
                view=error_notice("Not found", "This report no longer exists."),
                ephemeral=True,
            )
            return None
        ticket = await db.get_ticket(report.ticket_id)
        if ticket is None:
            await send_interaction(
                interaction,
                view=error_notice("Not found", "The related ticket no longer exists."),
                ephemeral=True,
            )
            return None
        return report, ticket

    async def _transition(
        self, interaction: discord.Interaction, action: str, new_status: str
    ) -> None:
        loaded = await self._load(interaction)
        if loaded is None:
            return
        report, ticket = loaded
        if report.status not in _VALID_TRANSITIONS[action]:
            await send_interaction(
                interaction,
                view=error_notice(
                    "Invalid status",
                    f"This report is **{report.status.title()}** — that action isn't available.",
                ),
                ephemeral=True,
            )
            return

        db = interaction.client.db
        updated = await db.set_report_status(report.id, new_status, interaction.user.id, now())
        if updated is None:
            await send_interaction(
                interaction,
                view=error_notice("Not found", "This report no longer exists."),
                ephemeral=True,
            )
            return

        if new_status == ReportStatus.RESOLVED:
            await self._post_resolve(interaction, ticket, updated)

        # Re-render the report message.
        fresh_ticket = await db.get_ticket(ticket.id) or ticket
        from views.report_views import build_report_data

        try:
            data = await build_report_data(interaction.client, updated, fresh_ticket)
        except Exception:
            log.exception("Could not rebuild report #%s card", updated.id)
            data = None

        if data is not None:
            view = ReportModView(
                updated.id,
                resolved=new_status == ReportStatus.RESOLVED,
                data=data,
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(view=view, embed=None)
                elif interaction.message is not None:
                    await interaction.message.edit(view=view, embed=None)
            except discord.HTTPException:
                log.exception("Could not update report #%s message", updated.id)

        await send_interaction(
            interaction,
            content=f"Report #{updated.id} is now **{new_status.title()}**.",
            ephemeral=True,
        )
        log.info("Report #%s -> %s by %s", updated.id, new_status, interaction.user.id)

    async def _post_resolve(
        self, interaction: discord.Interaction, ticket: Ticket, report: Report
    ) -> None:
        db = interaction.client.db
        # Restore a disputed ticket that is still live.
        if (
            ticket.status == TicketStatus.DISPUTED
            and report.prev_ticket_status
            and report.prev_ticket_status != TicketStatus.DISPUTED
            and report.prev_ticket_status in TicketStatus.ACTIVE
        ):
            await db.set_status(
                ticket.id,
                report.prev_ticket_status,
                now=now(),
                expect=TicketStatus.DISPUTED,
                actor_id=interaction.user.id,
            )
        if config.NOTIFY_REPORTER_ON_RESOLVE:
            from utils.checks import send_dm

            user = interaction.client.get_user(report.reporter_id)
            if user is None:
                try:
                    user = await interaction.client.fetch_user(report.reporter_id)
                except Exception:
                    user = None
            if user is not None:
                await send_dm(
                    user,
                    content=(
                        f"Your report #{report.id} on ticket {ticket.label} has been resolved "
                        "by the moderation team."
                    ),
                )

    # -- callbacks ------------------------------------------------------

    async def _on_ack(self, interaction: discord.Interaction) -> None:
        await self._transition(interaction, "ack", ReportStatus.ACKNOWLEDGED)

    async def _on_resolve(self, interaction: discord.Interaction) -> None:
        await self._transition(interaction, "resolve", ReportStatus.RESOLVED)

    async def _on_escalate(self, interaction: discord.Interaction) -> None:
        await self._transition(interaction, "escalate", ReportStatus.ESCALATED)


def register_report_view(bot, report_id: int, resolved: bool = False) -> None:
    try:
        bot.add_view(ReportModView(report_id, resolved=resolved))
    except (ValueError, TypeError):
        log.warning("Could not register report view #%s", report_id)
