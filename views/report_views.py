"""MM report flow: DM-based category select + details modal + evidence delivery."""

from __future__ import annotations

import logging
from typing import Optional

import discord

import config
from database.models import Report, Ticket, TicketStatus
from utils.checks import (
    load_ticket_for_view,
    safe_send,
    send_dm,
    send_interaction,
)
from utils.emojis import with_emoji
from utils.formatting import (
    error_notice,
    jump_link,
    role_label,
    status_label,
    success_notice,
    truncate,
)
from utils.time import format_ts, now
from views.base import (
    BaseModal,
    BaseView,
    PanelData,
    panel_container,
    panel_footer,
)

log = logging.getLogger("mm.views.report")

REPORTABLE_STATUSES = tuple(TicketStatus.CLAIMED | {TicketStatus.COMPLETED})


def _category_at(index: int) -> Optional[str]:
    if 0 <= index < len(config.REPORT_CATEGORIES):
        return config.REPORT_CATEGORIES[index]
    return None


def build_report_intro_data(ticket: Ticket) -> PanelData:
    fields = [("Ticket", ticket.label)]
    if ticket.claimed_mm_id:
        fields.append(("Reported MM", f"<@{ticket.claimed_mm_id}>"))
    return PanelData(
        title=with_emoji("report", "MM Report"),
        body="Which issue are you reporting?\n"
        "Pick a category below, then describe what happened.\n\n"
        "Your report goes only to the moderation team.",
        fields=fields,
        footer="Reports are private and never shown in the ticket.",
    )


# ---------------------------------------------------------------------------
# entry point (Report MM button)
# ---------------------------------------------------------------------------

async def start_report_flow(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(interaction, ticket_id)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if ticket.claimed_mm_id is None or ticket.status == TicketStatus.CLOSED:
        await send_interaction(
            interaction,
            view=error_notice(
                "No middleman",
                "There is no assigned middleman to report for this ticket right now.",
            ),
            ephemeral=True,
        )
        return
    if interaction.user.id not in (ticket.creator_id, ticket.partner_id):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not allowed", "Only the two traders in this ticket can report the middleman."
            ),
            ephemeral=True,
        )
        return
    if interaction.user.id == ticket.claimed_mm_id:
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "You cannot report yourself."),
            ephemeral=True,
        )
        return
    if not config.ALLOW_MULTIPLE_REPORTS and await db.has_report(ticket.id, interaction.user.id):
        await send_interaction(
            interaction,
            view=error_notice(
                "Already reported", "You have already submitted a report for this ticket."
            ),
            ephemeral=True,
        )
        return

    dm_ok = await send_dm(
        interaction.user,
        view=ReportCategoryView(ticket.id, data=build_report_intro_data(ticket)),
    )
    if dm_ok:
        await send_interaction(
            interaction,
            content="Please check your DMs to submit the report.",
            ephemeral=False,
            delete_after=config.PUBLIC_NOTICE_DELETE_AFTER or None,
        )
    else:
        log.info("DM closed for report flow by %s on %s", interaction.user.id, ticket.label)
        await send_interaction(
            interaction,
            content="I couldn't send you a DM. Please enable DMs from server members and try again.",
            ephemeral=False,
            delete_after=config.PUBLIC_NOTICE_DELETE_AFTER or None,
        )


# ---------------------------------------------------------------------------
# category select
# ---------------------------------------------------------------------------

class ReportCategoryView(BaseView):
    def __init__(self, ticket_id: int, *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        data = data or PanelData("MM Report")
        container = panel_container(data)
        options = []
        for index, category in enumerate(config.REPORT_CATEGORIES[:25]):
            options.append(
                discord.SelectOption(
                    label=category[:100],
                    value=str(index),
                    description=f"Report as: {category[:80]}" if index < 4 else None,
                )
            )
        select = discord.ui.Select(
            custom_id=f"mm:report_cat:{ticket_id}",
            placeholder="Which issue are you reporting?",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._on_select
        container.add_item(select)
        panel_footer(container, data)
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is not None:
            await send_interaction(
                interaction,
                view=error_notice("Unavailable", "Report from your direct messages with the bot."),
                ephemeral=True,
            )
            return
        values = (interaction.data or {}).get("values") or []
        try:
            index = int(values[0])
        except (ValueError, IndexError):
            await send_interaction(
                interaction, view=error_notice("Invalid choice", "Pick a category."), ephemeral=True
            )
            return
        category = _category_at(index)
        if category is None:
            await send_interaction(
                interaction, view=error_notice("Invalid choice", "Pick a category."), ephemeral=True
            )
            return

        db = interaction.client.db
        ticket = await db.get_ticket(self.ticket_id)
        if ticket is None:
            await send_interaction(
                interaction, view=error_notice("Unavailable", "This ticket no longer exists."), ephemeral=True
            )
            return
        if interaction.user.id not in (ticket.creator_id, ticket.partner_id):
            await send_interaction(
                interaction,
                view=error_notice("Not allowed", "You are not a trader in this ticket."),
                ephemeral=True,
            )
            return
        if not config.ALLOW_MULTIPLE_REPORTS and await db.has_report(ticket.id, interaction.user.id):
            await send_interaction(
                interaction,
                view=error_notice(
                    "Already reported", "You have already submitted a report for this ticket."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(ReportModal(self.ticket_id, index))


# ---------------------------------------------------------------------------
# details modal + submission
# ---------------------------------------------------------------------------

class ReportModal(BaseModal):
    def __init__(self, ticket_id: int, category_index: int):
        super().__init__(title="Report Details", ticket_id=ticket_id)
        self.category_index = category_index
        self.details = discord.ui.TextInput(
            label="Please explain what happened",
            custom_id=f"mm:report_details:{ticket_id}",
            style=discord.TextStyle.paragraph,
            placeholder="Include dates, amounts, links and anything that proves your case.",
            required=True,
            min_length=10,
            max_length=1000,
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        category = _category_at(self.category_index)
        if category is None:
            await send_interaction(
                interaction,
                view=error_notice("Invalid choice", "Please redo the report flow."),
                ephemeral=True,
            )
            return

        ticket = await db.get_ticket(self.ticket_id)
        if ticket is None:
            await send_interaction(
                interaction, view=error_notice("Unavailable", "This ticket no longer exists."), ephemeral=True
            )
            return
        if interaction.user.id not in (ticket.creator_id, ticket.partner_id):
            await send_interaction(
                interaction,
                view=error_notice("Not allowed", "You are not a trader in this ticket."),
                ephemeral=True,
            )
            return
        if ticket.claimed_mm_id is None:
            await send_interaction(
                interaction,
                view=error_notice(
                    "No middleman", "The middleman was released — there is nobody to report."
                ),
                ephemeral=True,
            )
            return

        description = str(self.details.value).strip()
        report, outcome = await db.create_report(
            ticket_id=ticket.id,
            reporter_id=interaction.user.id,
            reported_mm_id=ticket.claimed_mm_id,
            category=category,
            description=description,
            prev_ticket_status=ticket.status,
            now=now(),
        )
        if outcome == "duplicate" or report is None:
            await send_interaction(
                interaction,
                view=error_notice(
                    "Already reported", "You have already submitted a report for this ticket."
                ),
                ephemeral=True,
            )
            return

        # Flag disputes for scam-like categories while the ticket is live.
        if category in config.DISPUTE_CATEGORIES and ticket.status in TicketStatus.CLAIMED:
            await db.set_status(
                ticket.id,
                TicketStatus.DISPUTED,
                now=now(),
                expect=tuple(TicketStatus.CLAIMED),
                actor_id=interaction.user.id,
            )

        fresh = await db.get_ticket(ticket.id) or ticket
        report_data = await build_report_data(interaction.client, report, fresh)
        delivered = await _deliver_report(interaction.client, report, report_data)

        if delivered:
            await send_interaction(
                interaction,
                view=success_notice(
                    "Report submitted",
                    "Report submitted.\nOur moderation team has received your report.",
                ),
                ephemeral=True,
            )
            channel = None
            if interaction.client.get_guild(fresh.guild_id):
                guild = interaction.client.get_guild(fresh.guild_id)
                channel = guild.get_channel(fresh.channel_id) if fresh.channel_id else None
            if channel is not None:
                await safe_send(
                    channel,
                    content="Report submitted. Our moderation team has received your report.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await send_interaction(
                interaction,
                view=error_notice(
                    "Report recorded",
                    "Your report was recorded, but our moderation team could not be notified "
                    "right now. Staff will review it as soon as possible.",
                ),
                ephemeral=True,
            )
        log.info("Report #%s created for %s by %s", report.id, fresh.label, interaction.user.id)


async def _deliver_report(bot, report: Report, data: PanelData) -> bool:
    """Send the report to the restricted report channel. Never raises."""
    from views.staff_views import ReportModView  # local import: avoid cycles

    if not config.REPORT_CHANNEL_ID:
        log.error("REPORT_CHANNEL_ID is not configured — report #%s undelivered", report.id)
        return False
    channel = bot.get_channel(config.REPORT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.REPORT_CHANNEL_ID)
        except discord.HTTPException:
            channel = None
    if channel is None:
        log.error("Report channel %s unavailable — report #%s undelivered", config.REPORT_CHANNEL_ID, report.id)
        return False
    message = await safe_send(channel, view=ReportModView(report.id, data=data))
    if message is None:
        log.error("Could not deliver report #%s to report channel", report.id)
        return False
    try:
        await bot.db.attach_report_message(report.id, channel.id, message.id)
    except Exception:
        log.exception("Could not store delivery message for report #%s", report.id)
    return True


# ---------------------------------------------------------------------------
# evidence + full report embed
# ---------------------------------------------------------------------------

async def build_evidence(db, ticket: Ticket, report: Report) -> str:
    lines: list[str] = []
    lines.append(f"Ticket state at report time: {status_label(report.prev_ticket_status or ticket.status)}")

    traders = await db.get_traders(ticket.id)
    if traders:
        for trader in traders:
            if trader.trade_role:
                if trader.confirmed_at:
                    lines.append(
                        f"{role_label(trader.trade_role)} <@{trader.user_id}> confirmed {format_ts(trader.confirmed_at, 'R')}"
                    )
                else:
                    lines.append(f"{role_label(trader.trade_role)} <@{trader.user_id}> did not confirm")

    events = await db.get_events(ticket.id, limit=200)
    declines = [e for e in events if e.event_type == "TRADER_DECLINED"]
    for decline in declines:
        reason = decline.metadata.get("reason")
        text = f"Decline by <@{decline.actor_id}> {format_ts(decline.created_at, 'R')}"
        if reason:
            text += f" — {truncate(str(reason), 120)}"
        lines.append(text)

    claims = await db.get_claims(ticket.id)
    for claim in claims:
        if claim.released_at:
            lines.append(
                f"MM <@{claim.mm_id}> claimed {format_ts(claim.claimed_at, 'R')}, "
                f"released {format_ts(claim.released_at, 'R')}"
            )
        else:
            lines.append(f"MM <@{claim.mm_id}> claimed {format_ts(claim.claimed_at, 'R')} (active)")

    participants = [f"<@{ticket.creator_id}> ({ticket.creator_id})"]
    if ticket.partner_id:
        participants.append(f"<@{ticket.partner_id}> ({ticket.partner_id})")
    lines.append("Participants: " + ", ".join(participants))

    if config.INCLUDE_JUMP_LINKS:
        links = []
        for field, label in (
            ("status_message_id", "ticket panel"),
            ("confirm_message_id", "confirmation"),
            ("mm_message_id", "MM panel"),
        ):
            link = jump_link(ticket.guild_id, ticket.channel_id, getattr(ticket, field, None))
            if link:
                links.append(f"[{label}]({link})")
        if links:
            lines.append("Panels: " + " | ".join(links))

    return truncate("\n".join(lines), 1000)


async def build_report_data(bot, report: Report, ticket: Ticket) -> PanelData:
    """Full moderation report card (restricted channel only).

    Content is bounded (description 1000 + evidence 1000 + metadata), so the
    Components V2 4000-character limit is never approached.
    """
    db = bot.db
    fields: list[tuple[str, str]] = [
        ("Reporter", f"<@{report.reporter_id}>\nID: {report.reporter_id}"),
        ("Reported MM", f"<@{report.reported_mm_id}>\nID: {report.reported_mm_id}"),
    ]

    channel_text = f"<#{ticket.channel_id}>" if ticket.channel_id else "channel deleted"
    fields.append(
        (
            "Ticket",
            f"{ticket.label} (ID {ticket.id})\n"
            f"Channel: {channel_text}\n"
            f"Created: {format_ts(ticket.created_at, 'F')}",
        )
    )

    traders = await db.get_traders(ticket.id)
    buyer = seller = "—"
    for trader in traders:
        if trader.trade_role == "BUYER":
            buyer = f"<@{trader.user_id}> ({trader.user_id})"
        elif trader.trade_role == "SELLER":
            seller = f"<@{trader.user_id}> ({trader.user_id})"
    fields.append(("Trade Roles", f"Buyer: {buyer}\nSeller: {seller}"))

    fields.extend(
        (
            ("Category", report.category),
            ("Status", report.status.title()),
            ("Claimed At", format_ts(ticket.claimed_at, "F")),
            ("Reported At", format_ts(report.created_at, "F")),
            ("Description", truncate(report.description, 1000)),
        )
    )

    try:
        evidence = await build_evidence(db, ticket, report)
    except Exception:
        log.exception("Evidence collection failed for report #%s", report.id)
        evidence = "Evidence could not be collected automatically."
    fields.append(("Evidence", evidence))

    avatar_url = await _mm_avatar(bot, ticket.guild_id, report.reported_mm_id)
    return PanelData(
        title=with_emoji("report", "MM REPORT"),
        fields=fields,
        footer=f"Report #{report.id} • Ticket {ticket.label}",
        avatar_url=avatar_url,
    )


async def _mm_avatar(bot, guild_id: int, user_id: int) -> Optional[str]:
    guild = bot.get_guild(guild_id)
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member.display_avatar.url
    user = bot.get_user(user_id)
    if user is not None:
        return user.display_avatar.url
    try:
        user = await bot.fetch_user(user_id)
        return user.display_avatar.url
    except Exception:
        # Best-effort display data only — never fail report delivery over it.
        log.debug("Could not fetch avatar for %s", user_id, exc_info=True)
        return None
