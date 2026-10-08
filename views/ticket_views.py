"""Ticket lifecycle views: creation, partner modal, role selection, cancellation."""

from __future__ import annotations

import logging
from typing import Optional

import discord

import config
from database.models import EventType, Ticket, TicketStatus
from utils import permissions
from utils.checks import (
    load_ticket_for_view,
    parse_user_id,
    safe_send,
    send_interaction,
)
from utils.emojis import button_emoji
from utils.formatting import (
    error_notice,
    link_notice,
    success_notice,
    truncate,
    warning_notice,
)
from utils.time import format_duration, now
from views.base import (
    BaseModal,
    BaseView,
    PanelData,
    build_intro_data,
    build_terminal_data,
    generate_ticket_transcript,
    panel_container,
    panel_footer,
    plain_panel_view,
    refresh_status_panel,
    send_and_store,
)

log = logging.getLogger("mm.views.ticket")

ACTIVE_STATUSES = tuple(TicketStatus.ACTIVE)


# ---------------------------------------------------------------------------
# member resolution
# ---------------------------------------------------------------------------

async def resolve_guild_member(
    guild: discord.Guild, raw: str
) -> tuple[Optional[discord.Member], Optional[str]]:
    """Resolve an ID / mention / username to a guild member with friendly errors."""
    user_id = parse_user_id(raw)
    if user_id is not None:
        try:
            return await guild.fetch_member(user_id), None
        except discord.NotFound:
            return None, (
                "I couldn't find that user in this server. "
                "Double-check the ID or mention and try again."
            )
        except discord.HTTPException:
            log.exception("fetch_member failed for %s", user_id)
            return None, "I couldn't look up that user right now. Please try again in a moment."

    text = raw.strip().lstrip("@")
    if not text:
        return None, "Please provide a Discord ID or mention."

    lowered = text.lower()
    exact = [
        m
        for m in guild.members
        if m.name.lower() == lowered
        or (m.global_name or "").lower() == lowered
        or m.display_name.lower() == lowered
    ]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "That name matches multiple members. Please use their ID or mention instead."

    if guild.me is not None and guild.me.guild_permissions.members:
        try:
            results = await guild.query_members(query=text, limit=5, presences=False)
        except discord.HTTPException:
            results = []
        if len(results) == 1:
            return results[0], None
        if len(results) > 1:
            return None, "That name matches multiple members. Please use their ID or mention instead."

    return None, (
        "I couldn't find that user. Please use their Discord ID "
        "(e.g. `123456789012345678`) or mention them."
    )


# ---------------------------------------------------------------------------
# ticket channel creation
# ---------------------------------------------------------------------------

async def create_ticket_channel(
    guild: discord.Guild, ticket: Ticket, creator: discord.Member
) -> discord.TextChannel:
    """Create the private ticket channel; raises Forbidden/HTTPException to the caller."""
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        creator: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }
    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
        )

    role_grants = [
        (config.MM_ROLE_ID, config.TICKET_MM_SEES_TICKETS),
        (config.MM_ADMIN_ROLE_ID, config.TICKET_STAFF_SEES_TICKETS),
        (config.STAFF_ROLE_ID, config.TICKET_STAFF_SEES_TICKETS),
    ]
    seen_roles: set[int] = set()
    for role_id, enabled in role_grants:
        if not role_id or not enabled or role_id in seen_roles:
            continue
        seen_roles.add(role_id)
        role = guild.get_role(role_id)
        if role is None:
            log.warning("Configured role %s not found in guild %s", role_id, guild.id)
            continue
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )

    name = f"{config.TICKET_PREFIX}-{ticket.ticket_number:0{config.TICKET_NUMBER_PADDING}d}"
    reason = f"{ticket.label}: created by {creator} ({creator.id})"
    category = guild.get_channel(config.MM_CATEGORY_ID) if config.MM_CATEGORY_ID else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None
    if config.MM_CATEGORY_ID and category is None:
        log.warning("Configured category %s missing — creating ticket at guild root", config.MM_CATEGORY_ID)

    if category is not None:
        return await category.create_text_channel(name, overwrites=overwrites, reason=reason)
    return await guild.create_text_channel(name, overwrites=overwrites, reason=reason)


# ---------------------------------------------------------------------------
# ticket creation (setup panel button)
# ---------------------------------------------------------------------------

async def handle_create_ticket(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.member, discord.Member):
        await send_interaction(
            interaction,
            content="MM tickets can only be opened inside a server.",
            ephemeral=True,
        )
        return
    member = interaction.member
    if not permissions.can_create_tickets(member):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not authorized",
                "You don't have permission to open an MM ticket. Please contact staff.",
            ),
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not defer create interaction %s", interaction.id)
        return

    db = interaction.client.db
    try:
        ticket = await db.create_ticket(interaction.guild_id, member.id, now())
    except Exception as exc:  # noqa: PERF203 - domain errors mapped to friendly messages
        from database import CooldownError, TicketExistsError

        if isinstance(exc, TicketExistsError):
            existing = exc.ticket
            content = f"You already have an active MM ticket: **{existing.label}**."
            if existing.channel_id:
                url = f"https://discord.com/channels/{interaction.guild_id}/{existing.channel_id}"
                await interaction.followup.send(
                    view=link_notice(content, f"Open {existing.label}", url),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(content=content, ephemeral=True)
            return
        if isinstance(exc, CooldownError):
            await interaction.followup.send(
                f"You're creating tickets too quickly. Try again in **{format_duration(exc.retry_after)}**.",
                ephemeral=True,
            )
            return
        log.exception("Ticket creation failed for user %s", member.id)
        await interaction.followup.send(
            "I couldn't create a ticket due to an internal error. Please try again shortly.",
            ephemeral=True,
        )
        return

    # Channel creation --------------------------------------------------
    try:
        channel = await create_ticket_channel(interaction.guild, ticket, member)
    except discord.Forbidden:
        await db.close_ticket(ticket.id, member.id, now(), "Channel creation failed: missing permissions")
        log.error("Missing permissions to create ticket channel for %s", ticket.label)
        await interaction.followup.send(
            "I couldn't create a ticket channel — I'm missing permissions. Please contact staff.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await db.close_ticket(ticket.id, member.id, now(), "Channel creation failed: API error")
        log.exception("Channel creation failed for %s", ticket.label)
        await interaction.followup.send(
            "Discord refused to create the ticket channel. Please try again shortly.",
            ephemeral=True,
        )
        return

    await db.set_channel(ticket.id, channel.id, now())
    await db.set_status(ticket.id, TicketStatus.WAITING_FOR_PARTNER, now=now(), expect=TicketStatus.CREATED)
    ticket = await db.get_ticket(ticket.id) or ticket

    from views import register_ticket_views

    register_ticket_views(interaction.client, ticket.id)

    message = await send_and_store(
        db,
        channel,
        ticket,
        "status_message_id",
        view=TicketPanelView(ticket.id, stage="intro", data=build_intro_data(ticket)),
    )
    if message is None:
        log.error("Could not post intro panel for %s", ticket.label)

    url = f"https://discord.com/channels/{interaction.guild_id}/{channel.id}"
    await interaction.followup.send(
        view=link_notice(
            f"Your ticket **{ticket.label}** is ready. Add your trading partner to continue.",
            f"Open {ticket.label}",
            url,
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# add trading partner
# ---------------------------------------------------------------------------

async def handle_partner_button(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(
        interaction,
        ticket_id,
        statuses=(TicketStatus.CREATED, TicketStatus.WAITING_FOR_PARTNER),
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if interaction.user.id != ticket.creator_id:
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Only the ticket creator can add the trading partner."),
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(PartnerModal(ticket_id))


async def handle_partner_submit(
    interaction: discord.Interaction, ticket_id: int, raw_value: str
) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction,
        ticket_id,
        statuses=(TicketStatus.CREATED, TicketStatus.WAITING_FOR_PARTNER),
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if interaction.user.id != ticket.creator_id:
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "Only the ticket creator can add the trading partner."),
            ephemeral=True,
        )
        return
    if interaction.guild is None or not isinstance(interaction.member, discord.Member):
        await send_interaction(interaction, content="This must be used in a server.", ephemeral=True)
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        return

    member, resolve_error = await resolve_guild_member(interaction.guild, raw_value)
    if resolve_error or member is None:
        await interaction.followup.send(
            view=error_notice("Invalid trading partner", resolve_error or "Unknown user."),
            ephemeral=True,
        )
        return

    if member.id == ticket.creator_id:
        await interaction.followup.send(
            view=error_notice("Invalid trading partner", "You can't add yourself as your trading partner."),
            ephemeral=True,
        )
        return
    if member.bot:
        await interaction.followup.send(
            view=error_notice("Invalid trading partner", "Bots can't be trading partners."),
            ephemeral=True,
        )
        return
    if ticket.partner_id is not None:
        await interaction.followup.send(
            view=error_notice(
                "Already added",
                f"This ticket already has a trading partner (<@{ticket.partner_id}>).",
            ),
            ephemeral=True,
        )
        return
    if ticket.claimed_mm_id is not None and member.id == ticket.claimed_mm_id:
        await interaction.followup.send(
            view=error_notice("Invalid trading partner", "That user is the assigned middleman for this ticket."),
            ephemeral=True,
        )
        return
    if not config.ALLOW_PARTNER_WITH_ACTIVE_TICKET:
        other = await db.find_active_ticket_for_user(member.id)
        if other is not None:
            await interaction.followup.send(
                view=error_notice(
                    "Busy trader",
                    f"That user already has an active MM ticket (**{other.label}**). "
                    "They must finish it before joining another.",
                ),
                ephemeral=True,
            )
            return

    if not await db.add_partner(ticket.id, member.id, now()):
        await interaction.followup.send(
            view=error_notice(
                "Could not add partner",
                "This ticket's state changed — please refresh and try again.",
            ),
            ephemeral=True,
        )
        return

    # Grant channel access (best effort — never break the ticket over this).
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        except discord.HTTPException:
            log.warning(
                "Could not grant channel access to %s in ticket %s", member.id, ticket.label
            )

    ticket = await db.get_ticket(ticket.id) or ticket
    await db.set_status(
        ticket.id, TicketStatus.WAITING_FOR_ROLES, now=now(), expect=TicketStatus.PARTNER_ADDED
    )
    ticket = await db.get_ticket(ticket.id) or ticket

    await refresh_status_panel(interaction.client, ticket)

    public = await safe_send(
        interaction.channel,
        content=f"Trading partner added — <@{member.id}>\nBoth traders can now select their roles.",
        allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
    )
    if public is None:
        log.warning("Could not post partner confirmation in %s", ticket.label)
    await interaction.followup.send("Trading partner added.", ephemeral=True)


# ---------------------------------------------------------------------------
# role selection
# ---------------------------------------------------------------------------

async def handle_role_select(
    interaction: discord.Interaction, ticket_id: int, value: str
) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction,
        ticket_id,
        statuses=(
            TicketStatus.PARTNER_ADDED,
            TicketStatus.WAITING_FOR_ROLES,
            TicketStatus.ROLES_SELECTED,
            TicketStatus.WAITING_FOR_CONFIRMATION,
        ),
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return

    trader = await db.get_trader(ticket.id, interaction.user.id)
    if trader is None:
        await send_interaction(
            interaction,
            view=error_notice("Not a trader", "Only the two traders in this ticket can pick roles."),
            ephemeral=True,
        )
        return
    if value not in ("seller", "buyer"):
        await send_interaction(
            interaction, view=error_notice("Invalid choice", "Pick Seller or Buyer."), ephemeral=True
        )
        return
    if trader.trade_role is not None and not config.ALLOW_ROLE_CHANGE:
        await send_interaction(
            interaction,
            view=error_notice(
                "Selection locked",
                "Role changes are disabled for this ticket. Decline the confirmation to reset.",
            ),
            ephemeral=True,
        )
        return
    if ticket.status == TicketStatus.WAITING_FOR_CONFIRMATION and not config.ALLOW_ROLE_CHANGE:
        await send_interaction(
            interaction,
            view=error_notice(
                "Confirmation in progress",
                "Decline the confirmation before changing roles.",
            ),
            ephemeral=True,
        )
        return

    role = value.upper()
    updated = await db.set_trader_role(ticket.id, interaction.user.id, role, now())
    if updated is None:
        await send_interaction(
            interaction,
            view=error_notice("Unavailable", "Roles can't be changed for this ticket right now."),
            ephemeral=True,
        )
        return

    # Leaving the confirmation stage resets it (confirmations already cleared).
    if ticket.status == TicketStatus.WAITING_FOR_CONFIRMATION:
        await db.set_status(
            ticket.id,
            TicketStatus.ROLES_SELECTED,
            now=now(),
            expect=TicketStatus.WAITING_FOR_CONFIRMATION,
        )

    traders = await db.get_traders(ticket.id)
    from utils.checks import validate_role_pair

    state, conflict = validate_role_pair([t.trade_role for t in traders], config.ALLOW_SAME_ROLE)

    if state == "conflict":
        await db.set_status(
            ticket.id,
            TicketStatus.WAITING_FOR_ROLES,
            now=now(),
            expect=(
                TicketStatus.ROLES_SELECTED,
                TicketStatus.WAITING_FOR_ROLES,
                TicketStatus.PARTNER_ADDED,
            ),
        )
        ticket = await db.get_ticket(ticket.id) or ticket
        await send_interaction(interaction, view=error_notice("Invalid role pair", conflict), ephemeral=True)
        await refresh_status_panel(interaction.client, ticket, traders, note=conflict)
        await _park_confirmation_message(interaction.client, ticket, traders)
        return

    if state == "incomplete":
        await db.set_status(
            ticket.id,
            TicketStatus.WAITING_FOR_ROLES,
            now=now(),
            expect=(
                TicketStatus.PARTNER_ADDED,
                TicketStatus.ROLES_SELECTED,
                TicketStatus.WAITING_FOR_ROLES,
                TicketStatus.WAITING_FOR_CONFIRMATION,
            ),
        )
        ticket = await db.get_ticket(ticket.id) or ticket
        await send_interaction(
            interaction,
            view=success_notice(
                "Role saved",
                f"Your role is now **{'Seller' if role == 'SELLER' else 'Buyer'}**.",
            ),
            ephemeral=True,
        )
        await refresh_status_panel(interaction.client, ticket, traders)
        return

    # Valid pair -> start confirmation.
    started = await db.start_confirmation(
        ticket.id, now(), now() + config.CONFIRMATION_TIMEOUT, config.ALLOW_SAME_ROLE
    )
    if not started:
        ticket = await db.get_ticket(ticket.id) or ticket
        await send_interaction(
            interaction,
            view=error_notice(
                "Could not start confirmation",
                "The ticket state changed — please try again.",
            ),
            ephemeral=True,
        )
        await refresh_status_panel(interaction.client, ticket, traders)
        return

    ticket = await db.get_ticket(ticket.id) or ticket
    await send_interaction(
        interaction,
        view=success_notice(
            "Roles confirmed",
            "Both roles are set. The trade confirmation is now open — both traders must confirm.",
        ),
        ephemeral=True,
    )
    await refresh_status_panel(interaction.client, ticket, traders)

    from views.confirmation_views import sync_confirmation_message

    await sync_confirmation_message(interaction.client, ticket, traders)


async def _park_confirmation_message(bot, ticket: Ticket, traders) -> None:
    """After a role conflict, park the confirmation panel on a safe state."""
    from views.confirmation_views import sync_confirmation_message

    if ticket.confirm_message_id:
        await sync_confirmation_message(
            bot,
            ticket,
            traders,
            state="expired",
            stage="reset",
            detail="Role selection changed — pick valid, complementary roles to continue.",
        )


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------

def _cancel_authority(ticket: Ticket, member: Optional[discord.Member], user_id: int) -> bool:
    if user_id in (ticket.creator_id, ticket.partner_id):
        return True
    if ticket.claimed_mm_id is not None and user_id == ticket.claimed_mm_id:
        return True
    return permissions.is_staff(member)


async def handle_cancel(interaction: discord.Interaction, ticket_id: int) -> None:
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=ACTIVE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    if not _cancel_authority(ticket, member, interaction.user.id):
        await send_interaction(
            interaction,
            view=error_notice(
                "Not allowed",
                "Only a trader, the assigned middleman, or staff can cancel this ticket.",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(CancelModal(ticket_id))


async def handle_cancel_submit(
    interaction: discord.Interaction, ticket_id: int, reason: Optional[str]
) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(interaction, ticket_id, statuses=ACTIVE_STATUSES)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    member = interaction.member if isinstance(interaction.member, discord.Member) else None
    if not _cancel_authority(ticket, member, interaction.user.id):
        await send_interaction(
            interaction,
            view=error_notice("Not allowed", "You can no longer cancel this ticket."),
            ephemeral=True,
        )
        return

    needs_approval = (
        config.CANCEL_REQUIRES_APPROVAL
        and permissions.can_moderate_reports(member) is False
        and ticket.claimed_mm_id != interaction.user.id
    )
    if needs_approval:
        await db.add_event(
            ticket.id,
            interaction.user.id,
            EventType.CANCEL_REQUESTED,
            {"reason": reason},
            now(),
        )
        await send_interaction(
            interaction,
            view=warning_notice(
                "Cancellation requested",
                "The assigned middleman or staff must approve this cancellation.",
            ),
            ephemeral=True,
        )
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            approval_data = PanelData(
                title="Cancellation Requested",
                body=f"<@{interaction.user.id}> requested to cancel this ticket.\n"
                f"Reason: {truncate(reason, 300) if reason else '—'}",
                footer=f"Ticket ID {ticket_id}",
            )
            await safe_send(
                channel,
                view=CancelApprovalView(ticket_id, data=approval_data),
            )
        return

    await apply_cancel(interaction, ticket, interaction.user.id, reason)


async def apply_cancel(
    interaction: discord.Interaction, ticket: Ticket, actor_id: int, reason: Optional[str]
) -> None:
    """Cancel the ticket, disable panels and generate the transcript."""
    db = interaction.client.db
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    if not await db.cancel_ticket(ticket.id, actor_id, now(), reason):
        await interaction.followup.send(
            view=error_notice("Unavailable", "This ticket can no longer be cancelled."),
            ephemeral=True,
        )
        return

    ticket = await db.get_ticket(ticket.id) or ticket
    guild = interaction.guild
    if guild is not None:
        from views.base import edit_panel

        terminal = build_terminal_data(ticket)
        for field_name in ("status_message_id", "confirm_message_id", "mm_message_id"):
            await edit_panel(
                guild,
                ticket,
                field_name,
                build=lambda data=terminal: plain_panel_view(data),
            )

    await interaction.followup.send(
        view=success_notice(
            "Ticket cancelled",
            f"**{ticket.label}** has been cancelled and its controls disabled.",
        ),
        ephemeral=True,
    )
    await generate_ticket_transcript(interaction.client, ticket)


# ---------------------------------------------------------------------------
# cancellation approval (MM / staff)
# ---------------------------------------------------------------------------

class CancelApprovalView(BaseView):
    def __init__(self, ticket_id: int, *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        data = data or PanelData("Cancellation Requested")
        container = panel_container(data)
        approve = discord.ui.Button(
            label="Approve Cancellation",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm:cancel_approve:{ticket_id}",
            emoji=button_emoji("confirm"),
        )
        approve.callback = self._on_approve
        container.add_item(approve)
        deny = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm:cancel_deny:{ticket_id}",
            emoji=button_emoji("decline"),
        )
        deny.callback = self._on_deny
        container.add_item(deny)
        panel_footer(container, data)
        self.add_item(container)

    async def _is_approver(self, interaction: discord.Interaction) -> bool:
        ticket, error = await load_ticket_for_view(
            interaction, self.ticket_id, statuses=ACTIVE_STATUSES
        )
        if error:
            await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
            return False
        member = interaction.member if isinstance(interaction.member, discord.Member) else None
        is_staff = permissions.can_moderate_reports(member)
        is_mm = ticket.claimed_mm_id is not None and interaction.user.id == ticket.claimed_mm_id
        if not (is_staff or is_mm):
            await send_interaction(
                interaction,
                view=error_notice("Not allowed", "Only the assigned middleman or staff can decide this."),
                ephemeral=True,
            )
            return False
        if interaction.user.id == ticket.creator_id or interaction.user.id == ticket.partner_id:
            await send_interaction(
                interaction,
                view=error_notice("Not allowed", "You requested this cancellation — someone else must approve it."),
                ephemeral=True,
            )
            return False
        return True

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        if not await self._is_approver(interaction):
            return
        db = interaction.client.db
        ticket = await db.get_ticket(self.ticket_id)
        if ticket is None:
            await send_interaction(interaction, content="This ticket no longer exists.", ephemeral=True)
            return
        reason = None
        events = await db.get_events(ticket.id, limit=30)
        for event in reversed(events):
            if event.event_type == EventType.CANCEL_REQUESTED:
                reason = event.metadata.get("reason")
                break
        await apply_cancel(interaction, ticket, interaction.user.id, reason)

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        if not await self._is_approver(interaction):
            return
        db = interaction.client.db
        ticket = await db.get_ticket(self.ticket_id)
        if ticket is None:
            await send_interaction(interaction, content="This ticket no longer exists.", ephemeral=True)
            return
        await db.add_event(
            ticket.id,
            interaction.user.id,
            EventType.CANCEL_DENIED,
            {},
            now(),
        )
        await send_interaction(
            interaction,
            view=warning_notice(
                "Cancellation denied",
                "The cancellation request was denied. The ticket continues normally.",
            ),
            ephemeral=True,
        )
        if interaction.message is not None:
            denied = PanelData(
                title="Cancellation denied",
                body=f"Denied by <@{interaction.user.id}>. The ticket continues normally.",
            )
            try:
                await interaction.message.edit(
                    view=plain_panel_view(denied), embed=None
                )
            except discord.HTTPException:
                log.warning("Could not edit approval message for ticket %s", ticket.label)


# ---------------------------------------------------------------------------
# modals
# ---------------------------------------------------------------------------

class PartnerModal(BaseModal):
    def __init__(self, ticket_id: int):
        super().__init__(title="Add Trading Partner", ticket_id=ticket_id)
        self.input = discord.ui.TextInput(
            label="Discord ID or Mention",
            custom_id=f"mm:partner_input:{ticket_id}",
            placeholder="e.g. 123456789012345678 or @user",
            min_length=2,
            max_length=120,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await handle_partner_submit(interaction, self.ticket_id, str(self.input.value))


class CancelModal(BaseModal):
    def __init__(self, ticket_id: int):
        super().__init__(title="Cancel this ticket?", ticket_id=ticket_id)
        self.reason_input = discord.ui.TextInput(
            label="Reason (optional)",
            custom_id=f"mm:cancel_reason:{ticket_id}",
            placeholder="Tell the other trader why you're cancelling",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason_input.value).strip() or None
        await handle_cancel_submit(interaction, self.ticket_id, reason)


# ---------------------------------------------------------------------------
# ticket panel view (status message across its lifecycle)
# ---------------------------------------------------------------------------

class TicketPanelView(BaseView):
    """Main ticket panel: add partner -> role select -> flat status.

    Stage ``all`` registers every custom_id once for restart recovery.
    """

    def __init__(self, ticket_id: int, stage: str = "all", *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        self.stage = stage
        data = data or PanelData("Ticket")
        container = panel_container(data)

        if stage in ("intro", "all"):
            partner = discord.ui.Button(
                label="Add Trading Partner",
                style=discord.ButtonStyle.primary,
                custom_id=f"mm:add_partner:{ticket_id}",
                emoji=button_emoji("partner"),
            )
            partner.callback = self._on_partner
            container.add_item(partner)

        if stage in ("roles", "all"):
            select = discord.ui.Select(
                custom_id=f"mm:role:{ticket_id}",
                placeholder="Choose your trading role",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label="Seller",
                        value="seller",
                        description="You are selling",
                        emoji=button_emoji("seller"),
                    ),
                    discord.SelectOption(
                        label="Buyer",
                        value="buyer",
                        description="You are buying",
                        emoji=button_emoji("buyer"),
                    ),
                ],
            )
            select.callback = self._on_role
            container.add_item(select)

        if stage != "none":
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

    async def _on_partner(self, interaction: discord.Interaction) -> None:
        await handle_partner_button(interaction, self.ticket_id)

    async def _on_role(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        value = values[0] if values else ""
        await handle_role_select(interaction, self.ticket_id, value)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await handle_cancel(interaction, self.ticket_id)
