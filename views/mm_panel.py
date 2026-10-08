"""MM panel views: setup panel, claim button, assigned-MM panel, release/complete."""

from __future__ import annotations

import logging
from typing import Optional

import discord

import config
from database.models import Ticket, TicketStatus
from utils import permissions
from utils.checks import load_ticket_for_view, safe_send, send_dm, send_interaction
from utils.emojis import button_emoji, with_emoji
from utils.formatting import error_notice, notice, success_notice, warning_notice
from utils.time import now
from views.base import (
    BaseView,
    PanelData,
    build_assigned_data,
    build_intro_data,
    build_mm_request_data,
    build_profile_data,
    build_status_data,
    build_terminal_data,
    edit_panel,
    generate_ticket_transcript,
    panel_container,
    panel_footer,
    panel_status,
    plain_panel_view,
    refresh_status_panel,
    send_and_store,
    stage_for_status,
)

log = logging.getLogger("mm.views.mm_panel")

CLAIMABLE_STATUSES = tuple(TicketStatus.CLAIMED)
PROFILE_STATUSES = tuple(TicketStatus.CLAIMED | {TicketStatus.COMPLETED})


def mm_request_ping(guild: discord.Guild) -> tuple[Optional[str], Optional[discord.AllowedMentions]]:
    """Role ping content for MM request messages."""
    if not config.MM_ROLE_ID:
        return None, None
    role = guild.get_role(config.MM_ROLE_ID)
    if role is None:
        log.warning("MM role %s missing in guild %s", config.MM_ROLE_ID, guild.id)
        return None, None
    return role.mention, discord.AllowedMentions(roles=[role], users=False, everyone=False)


async def _display_for(bot, guild: Optional[discord.Guild], user_id: int) -> tuple[str, Optional[str]]:
    """(username_text, avatar_url) with cache-first, fetch fallback."""
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return str(member), member.display_avatar.url
    user = bot.get_user(user_id)
    if user is not None:
        return str(user), user.display_avatar.url
    try:
        user = await bot.fetch_user(user_id)
        return str(user), user.display_avatar.url
    except Exception:
        log.warning("Could not fetch user %s for MM display", user_id, exc_info=True)
        return str(user_id), None


# ---------------------------------------------------------------------------
# setup panel
# ---------------------------------------------------------------------------

class SetupPanelView(BaseView):
    """Global setup panel (registered once at startup).

    Borderless Components V2 container matching the reference design:
    heading, intro lines, the create button, then the must-read links.
    """

    def __init__(self):
        super().__init__(ticket_id=None)
        container = discord.ui.Container()  # accent_color=None -> no accent border
        container.add_item(
            discord.ui.TextDisplay(f"## {with_emoji('mm', 'MM Escrow')}\nMiddleman tickets")
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(
                    (
                        with_emoji("success", "Open a ticket below."),
                        with_emoji(
                            "contact",
                            "Ping your partner or paste their `Discord ID`.",
                        ),
                        with_emoji(
                            "security",
                            "Only both traders and staff can view the ticket.",
                        ),
                    )
                )
            )
        )
        create = discord.ui.Button(
            label="Create MM Ticket",
            style=discord.ButtonStyle.success,
            custom_id="mm:create",
            emoji=button_emoji("ticket"),
        )
        create.callback = self._on_create
        container.add_item(create)

        parts = [
            f"[{label}]({url})"
            for label, url in (
                ("MM ToS", config.MM_TOS_URL),
                ("Guidelines", config.MM_GUIDELINES_URL),
            )
            if url
        ]
        if parts:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    f"{with_emoji('warning', 'Must read:')} {' · '.join(parts)}"
                )
            )
        self.add_item(container)

    async def _on_create(self, interaction: discord.Interaction) -> None:
        from views.ticket_views import handle_create_ticket  # local import: avoid cycles

        await handle_create_ticket(interaction)


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

class ClaimView(BaseView):
    def __init__(self, ticket_id: int, *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        data = data or PanelData("MM Required")
        container = panel_container(data)
        claim = discord.ui.Button(
            label="Claim MM",
            style=discord.ButtonStyle.primary,
            custom_id=f"mm:claim:{ticket_id}",
            emoji=button_emoji("money"),
        )
        claim.callback = self._on_claim
        container.add_item(claim)
        panel_footer(container, data)
        self.add_item(container)

    async def _on_claim(self, interaction: discord.Interaction) -> None:
        await handle_claim(interaction, self.ticket_id)


async def apply_claimed_status(db, ticket: Ticket) -> Ticket:
    """Optionally advance MM_CLAIMED -> IN_PROGRESS (config.AUTO_IN_PROGRESS)."""
    if config.AUTO_IN_PROGRESS and ticket.status == TicketStatus.MM_CLAIMED:
        await db.set_status(
            ticket.id, TicketStatus.IN_PROGRESS, now=now(), expect=TicketStatus.MM_CLAIMED
        )
        ticket = await db.get_ticket(ticket.id) or ticket
    return ticket


async def handle_claim(interaction: discord.Interaction, ticket_id: int) -> None:
    """Atomic MM claim — verifies roles, traders and state, then claims in one tx."""
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction, ticket_id, statuses=(TicketStatus.WAITING_FOR_MM,)
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return

    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    if member is None:
        await send_interaction(
            interaction, content="This action can only be used in a server.", ephemeral=True
        )
        return
    if interaction.user.id in (ticket.creator_id, ticket.partner_id):
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Traders cannot claim their own ticket."),
            ephemeral=True,
        )
        return
    if member.bot or not permissions.can_claim_mm(member):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not authorized",
                "You need the middleman role to claim this ticket.",
            ),
            ephemeral=True,
        )
        return

    result = await db.claim_ticket(ticket.id, member.id, now())
    if not result.ok:
        if result.claimed_by:
            message = f"This ticket has already been claimed by <@{result.claimed_by}>."
        elif result.status:
            message = "This ticket is no longer available to claim."
        else:
            message = "This ticket no longer exists."
        await send_interaction(interaction, view=error_notice("Already claimed", message), ephemeral=True)
        return

    fresh = await db.get_ticket(ticket.id) or ticket
    fresh = await apply_claimed_status(db, fresh)
    view = AssignedView(
        ticket.id, data=build_assigned_data(fresh, member.id, member.display_avatar.url)
    )
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(content=None, view=view, embed=None)
        elif interaction.message is not None:
            await interaction.message.edit(content=None, view=view, embed=None)
        else:
            await send_interaction(interaction, view=view, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not update MM panel for %s", fresh.label)
        await send_interaction(
            interaction,
            view=error_notice("Panel update failed", "You claimed the ticket, but the panel could not update."),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        view=success_notice(
            "Ticket claimed",
            f"You are now the middleman for **{fresh.label}**.",
        ),
        ephemeral=True,
    )
    log.info("MM %s claimed %s", member.id, fresh.label)


# ---------------------------------------------------------------------------
# assigned panel
# ---------------------------------------------------------------------------

class AssignedView(BaseView):
    """Assigned-MM panel.

    Stages: ``full`` (all controls), ``finished`` (profile/report only),
    ``all`` (registry: everything for restart recovery).
    """

    def __init__(self, ticket_id: int, stage: str = "all", *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        self.stage = stage
        full = stage in ("full", "all")
        data = data or PanelData("Middleman Assigned")
        container = panel_container(data)

        profile = discord.ui.Button(
            label="MM Profile",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm:profile:{ticket_id}",
            emoji=button_emoji("profile"),
        )
        profile.callback = self._on_profile
        container.add_item(profile)

        report = discord.ui.Button(
            label="Report MM",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm:report:{ticket_id}",
            emoji=button_emoji("report"),
        )
        report.callback = self._on_report
        container.add_item(report)

        if full or stage == "all":
            contact = discord.ui.Button(
                label="Contact MM",
                style=discord.ButtonStyle.secondary,
                custom_id=f"mm:contact:{ticket_id}",
                emoji=button_emoji("contact"),
            )
            contact.callback = self._on_contact
            container.add_item(contact)

            release = discord.ui.Button(
                label="Release MM",
                style=discord.ButtonStyle.secondary,
                custom_id=f"mm:release:{ticket_id}",
                emoji=button_emoji("release"),
            )
            release.callback = self._on_release
            container.add_item(release)

            complete = discord.ui.Button(
                label="Complete Trade",
                style=discord.ButtonStyle.success,
                custom_id=f"mm:complete:{ticket_id}",
                emoji=button_emoji("complete"),
            )
            complete.callback = self._on_complete
            container.add_item(complete)

        if full or stage == "all":
            cancel = discord.ui.Button(
                label="Cancel Ticket",
                style=discord.ButtonStyle.danger,
                custom_id=f"mm:cancel:{ticket_id}",
                emoji=button_emoji("cancel"),
            )
            cancel.callback = self._on_cancel
            container.add_item(cancel)

        panel_footer(container, data)
        self.add_item(container)

    async def _on_profile(self, interaction: discord.Interaction) -> None:
        await handle_profile(interaction, self.ticket_id)

    async def _on_report(self, interaction: discord.Interaction) -> None:
        from views.report_views import start_report_flow  # local import

        await start_report_flow(interaction, self.ticket_id)

    async def _on_contact(self, interaction: discord.Interaction) -> None:
        await handle_contact(interaction, self.ticket_id)

    async def _on_release(self, interaction: discord.Interaction) -> None:
        await handle_release_request(interaction, self.ticket_id)

    async def _on_complete(self, interaction: discord.Interaction) -> None:
        await handle_complete_request(interaction, self.ticket_id)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        from views.ticket_views import handle_cancel  # local import

        await handle_cancel(interaction, self.ticket_id)


def _is_assigned_or_staff(ticket: Ticket, interaction: discord.Interaction) -> bool:
    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    if member is None:
        return False
    if ticket.claimed_mm_id is not None and interaction.user.id == ticket.claimed_mm_id:
        return True
    return permissions.can_moderate_reports(member)


async def handle_profile(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=PROFILE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if ticket.claimed_mm_id is None:
        await send_interaction(
            interaction,
            view=error_notice("No middleman", "No middleman is assigned to this ticket."),
            ephemeral=True,
        )
        return
    username, avatar_url = await _display_for(interaction.client, interaction.guild, ticket.claimed_mm_id)
    await send_interaction(
        interaction,
        view=plain_panel_view(
            build_profile_data(ticket, ticket.claimed_mm_id, username, avatar_url)
        ),
        ephemeral=True,
    )


async def handle_contact(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=CLAIMABLE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    is_trader = interaction.user.id in (ticket.creator_id, ticket.partner_id)
    if not (is_trader or permissions.can_moderate_reports(member)):
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Only traders in this ticket can contact the middleman."),
            ephemeral=True,
        )
        return
    if ticket.claimed_mm_id is None:
        await send_interaction(
            interaction,
            view=error_notice("No middleman", "No middleman is assigned to this ticket."),
            ephemeral=True,
        )
        return

    mm_id = ticket.claimed_mm_id
    target = None
    if interaction.guild is not None:
        target = interaction.guild.get_member(mm_id)
    if target is None:
        target = interaction.client.get_user(mm_id)
    if target is None:
        try:
            target = await interaction.client.fetch_user(mm_id)
        except Exception:
            target = None

    dm_ok = False
    if target is not None:
        body = (
            f"A trader in **{ticket.label}** would like to contact you.\n"
            f"Ticket: <#{ticket.channel_id}>"
            if ticket.channel_id
            else f"A trader in **{ticket.label}** would like to contact you."
        )
        dm_ok = await send_dm(target, view=notice("Contact requested", body))

    if dm_ok:
        await send_interaction(
            interaction,
            content=f"<@{mm_id}>, please check your DMs — the traders in this ticket would like to contact you.",
            ephemeral=False,
        )
    else:
        await send_interaction(
            interaction,
            content=f"I couldn't DM the middleman. <@{mm_id}>, the traders in this ticket would like to "
            "contact you — please reach out in this channel.",
            ephemeral=False,
        )


async def handle_release_request(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=CLAIMABLE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if not _is_assigned_or_staff(ticket, interaction):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not allowed", "Only the assigned middleman or staff can release this ticket."
            ),
            ephemeral=True,
        )
        return
    await send_interaction(
        interaction,
        view=ReleaseConfirmView(
            ticket_id,
            data=PanelData(
                title="Release this ticket?",
                body="The traders will be notified and the configured MM role will be pinged again.\n\n"
                "This cannot be undone.",
            ),
        ),
        ephemeral=True,
    )


async def handle_release_confirm(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=CLAIMABLE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if not _is_assigned_or_staff(ticket, interaction):
        await send_interaction(
            interaction, view=error_notice("Not allowed", "You can no longer do this."), ephemeral=True
        )
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        return

    if not await db.release_ticket(ticket.id, interaction.user.id, now(), reason="released via panel"):
        await interaction.followup.send(
            view=error_notice("Unavailable", "This ticket can no longer be released."), ephemeral=True
        )
        return

    fresh = await db.get_ticket(ticket.id) or ticket
    guild = interaction.guild
    if guild is not None:
        content, allowed = mm_request_ping(guild)
        await edit_panel(
            guild,
            fresh,
            "mm_message_id",
            content=content,
            build=lambda: ClaimView(fresh.id, data=build_mm_request_data(fresh)),
            allowed_mentions=allowed,
        )
        await refresh_status_panel(interaction.client, fresh)

    await interaction.followup.send(
        view=warning_notice(
            "Ticket released",
            f"**{fresh.label}** is open again — a new middleman can claim it.",
        ),
        ephemeral=True,
    )
    log.info("MM released %s (actor %s)", fresh.label, interaction.user.id)


async def handle_complete_request(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=CLAIMABLE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if not _is_assigned_or_staff(ticket, interaction):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not allowed", "Only the assigned middleman or staff can complete this trade."
            ),
            ephemeral=True,
        )
        return
    await send_interaction(
        interaction,
        view=CompleteConfirmView(
            ticket_id,
            data=PanelData(
                title="Complete this MM ticket?",
                body="Only use this once the transaction is finished.\n"
                "The ticket will be marked completed, controls disabled and the audit history preserved.",
            ),
        ),
        ephemeral=True,
    )


async def handle_complete_confirm(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=CLAIMABLE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if not _is_assigned_or_staff(ticket, interaction):
        await send_interaction(
            interaction, view=error_notice("Not allowed", "You can no longer do this."), ephemeral=True
        )
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        return

    if not await db.complete_ticket(ticket.id, interaction.user.id, now()):
        await interaction.followup.send(
            view=error_notice("Unavailable", "This trade can no longer be completed."), ephemeral=True
        )
        return

    fresh = await db.get_ticket(ticket.id) or ticket
    guild = interaction.guild
    if guild is not None:
        terminal = build_terminal_data(fresh)
        await edit_panel(
            guild,
            fresh,
            "mm_message_id",
            build=lambda: AssignedView(fresh.id, stage="finished", data=terminal),
        )
        for field_name in ("status_message_id", "confirm_message_id"):
            await edit_panel(
                guild,
                fresh,
                field_name,
                build=lambda data=terminal: plain_panel_view(data),
            )

    await interaction.followup.send(
        view=success_notice(
            "Trade completed",
            f"**{fresh.label}** has been marked completed. The audit history is preserved.",
        ),
        ephemeral=True,
    )
    log.info("Trade completed for %s by %s", fresh.label, interaction.user.id)
    await generate_ticket_transcript(interaction.client, fresh)


# ---------------------------------------------------------------------------
# confirmation views (ephemeral, two-step)
# ---------------------------------------------------------------------------

class ReleaseConfirmView(BaseView):
    def __init__(self, ticket_id: int, *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        data = data or PanelData("Release this ticket?")
        container = panel_container(data)
        go = discord.ui.Button(
            label="Confirm Release",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm:release_go:{ticket_id}",
            emoji=button_emoji("release"),
        )
        go.callback = self._on_go
        container.add_item(go)
        stop = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm:release_stop:{ticket_id}",
        )
        stop.callback = self._on_stop
        container.add_item(stop)
        panel_footer(container, data)
        self.add_item(container)

    async def _on_go(self, interaction: discord.Interaction) -> None:
        await handle_release_confirm(interaction, self.ticket_id)

    async def _on_stop(self, interaction: discord.Interaction) -> None:
        closed = plain_panel_view(
            PanelData("Release cancelled", "The ticket stays with the current middleman.")
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=closed, embed=None)
            elif interaction.message is not None:
                await interaction.message.edit(view=closed, embed=None)
        except discord.HTTPException:
            log.warning("Could not close release confirmation for ticket %s", self.ticket_id)


class CompleteConfirmView(BaseView):
    def __init__(self, ticket_id: int, *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        data = data or PanelData("Complete this MM ticket?")
        container = panel_container(data)
        go = discord.ui.Button(
            label="Confirm Complete",
            style=discord.ButtonStyle.success,
            custom_id=f"mm:complete_go:{ticket_id}",
            emoji=button_emoji("complete"),
        )
        go.callback = self._on_go
        container.add_item(go)
        stop = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm:complete_stop:{ticket_id}",
        )
        stop.callback = self._on_stop
        container.add_item(stop)
        panel_footer(container, data)
        self.add_item(container)

    async def _on_go(self, interaction: discord.Interaction) -> None:
        await handle_complete_confirm(interaction, self.ticket_id)

    async def _on_stop(self, interaction: discord.Interaction) -> None:
        closed = plain_panel_view(
            PanelData("Completion cancelled", "The ticket remains active.")
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=closed, embed=None)
            elif interaction.message is not None:
                await interaction.message.edit(view=closed, embed=None)
        except discord.HTTPException:
            log.warning("Could not close completion confirmation for ticket %s", self.ticket_id)


# ---------------------------------------------------------------------------
# restart recovery
# ---------------------------------------------------------------------------

async def restore_ticket_panels(bot, ticket: Ticket) -> None:
    """Rebuild missing panels after a restart and recover interrupted flows.

    Never deletes data; every step is best-effort and logged.
    """
    db = bot.db
    guild = bot.get_guild(ticket.guild_id)
    if guild is None:
        return

    # 1) Crash window: both confirmed but MM request never sent.
    if ticket.status == TicketStatus.CONFIRMED:
        if await db.begin_mm_request(ticket.id, now()):
            ticket = await db.get_ticket(ticket.id) or ticket
            log.info("Recovered confirmed ticket %s -> waiting for MM", ticket.label)

    channel_missing = ticket.channel_id is None or guild.get_channel(ticket.channel_id) is None
    if channel_missing and ticket.is_active:
        await _flag_channel_missing(db, ticket, guild)
        return

    # 2) WAITING_FOR_MM without a request panel.
    if ticket.status == TicketStatus.WAITING_FOR_MM:
        if ticket.mm_message_id is None:
            from views.confirmation_views import send_mm_request

            await send_mm_request(bot, ticket)
            ticket = await db.get_ticket(ticket.id) or ticket
        elif await panel_status(guild, ticket, "mm_message_id") is False:
            from views.confirmation_views import send_mm_request

            await send_mm_request(bot, ticket, force=True)
            ticket = await db.get_ticket(ticket.id) or ticket

    # 3) Status panel (Msg1).
    traders = await db.get_traders(ticket.id)
    if await panel_status(guild, ticket, "status_message_id") is False:
        from views.ticket_views import TicketPanelView

        stage = stage_for_status(ticket.status)
        if stage == "intro":
            data = build_intro_data(ticket)
        else:
            data = build_status_data(ticket, traders)
        channel = guild.get_channel(ticket.channel_id)
        if channel is not None:
            await send_and_store(
                db, channel, ticket, "status_message_id",
                view=TicketPanelView(ticket.id, stage=stage, data=data),
            )
            log.info("Rebuilt status panel for %s", ticket.label)

    # 4) Confirmation panel (Msg2) while confirmation is live.
    if ticket.status in (TicketStatus.WAITING_FOR_CONFIRMATION, TicketStatus.CONFIRMED):
        if await panel_status(guild, ticket, "confirm_message_id") is False:
            from views.confirmation_views import sync_confirmation_message

            if ticket.status == TicketStatus.CONFIRMED:
                await sync_confirmation_message(bot, ticket, traders, state="confirmed")
            else:
                await sync_confirmation_message(bot, ticket, traders, state="pending")
            log.info("Rebuilt confirmation panel for %s", ticket.label)

    # 5) Assigned panel (Msg3) when an MM holds the ticket.
    if ticket.status in TicketStatus.CLAIMED and ticket.claimed_mm_id:
        exists = await panel_status(guild, ticket, "mm_message_id")
        if exists is False:
            channel = guild.get_channel(ticket.channel_id)
            if channel is not None:
                member = guild.get_member(ticket.claimed_mm_id)
                avatar = member.display_avatar.url if member else None
                await send_and_store(
                    db, channel, ticket, "mm_message_id",
                    view=AssignedView(
                        ticket.id,
                        stage="full",
                        data=build_assigned_data(ticket, ticket.claimed_mm_id, avatar),
                    ),
                )
                log.info("Rebuilt assigned MM panel for %s", ticket.label)


async def _flag_channel_missing(db, ticket: Ticket, guild: discord.Guild) -> None:
    """Record (once) that a live ticket lost its channel; staff can recover it."""
    if not await db.has_event(ticket.id, "CHANNEL_MISSING"):
        await db.add_event(ticket.id, None, "CHANNEL_MISSING", {"channel_id": ticket.channel_id}, now())
        log.warning("Ticket %s channel %s is missing", ticket.label, ticket.channel_id)
    await db.set_channel_missing(ticket.id)

    if config.STAFF_LOG_CHANNEL_ID:
        channel = guild.get_channel(config.STAFF_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.STAFF_LOG_CHANNEL_ID)
            except discord.HTTPException:
                channel = None
        if channel is not None:
            await safe_send(
                channel,
                content=(
                    f"Ticket **{ticket.label}** (ID {ticket.id}) has no accessible channel. "
                    f"Use `/mm ticketinfo` and `/mm close` to recover it."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
