"""Shared view infrastructure (Components V2).

- ``BaseView``: persistent :class:`discord.ui.LayoutView` base with friendly
  error handling — every panel in the bot is a Components V2 layout message
  (a borderless ``Container`` of text, separators and controls).
- ``PanelData`` + builders: panel content as structured data, rendered into
  containers by :func:`panel_container` / :func:`panel_footer`.
- Panel message helpers (send/edit/store with database message ids). Edits
  detect legacy embed-only messages and replace them, since components_v2
  cannot be added to an existing message.
- Transcript generation used by completion/cancellation/staff close.

Cross-module references between ``views.*`` modules use function-level
imports to keep import cycles impossible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import discord

import config
from database.models import Ticket, TicketStatus, Trader
from utils.checks import safe_edit, safe_send
from utils.emojis import with_emoji
from utils.formatting import (
    build_transcript,
    collect_channel_messages,
    deliver_transcript,
    role_label,
    status_label,
    truncate,
)
from utils.time import format_ts

log = logging.getLogger("mm.views")


class BaseView(discord.ui.LayoutView):
    """Persistent layout view (timeout=None) with friendly error responses.

    Inherits :class:`discord.ui.LayoutView`, so ``add_item`` accepts V2
    components (Container/TextDisplay/Separator/Section) as well as the
    buttons and selects nested inside them.
    """

    def __init__(self, ticket_id: Optional[int] = None, **kwargs):
        super().__init__(timeout=None, **kwargs)
        self.ticket_id = ticket_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, *args, **kwargs) -> None:
        # Tracebacks go to the log only — never to users.
        # (*args absorbs the item parameter passed by discord.py 2.6+)
        log.exception(
            "View error in %s (ticket=%s)", type(self).__name__, self.ticket_id, exc_info=error
        )
        await respond_view_error(interaction, error)


async def respond_view_error(interaction: discord.Interaction, error: Exception) -> None:
    """Best-effort ephemeral error reply; never raises."""
    if isinstance(error, discord.Forbidden):
        message = "I don't have permission to do that. Please contact staff."
    elif isinstance(error, discord.NotFound):
        message = "That no longer exists — it may have been deleted."
    elif isinstance(error, discord.HTTPException):
        message = "Discord is having trouble right now. Please try again in a moment."
    else:
        message = "Something went wrong while processing that action. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not report view error to user")


class BaseModal(discord.ui.Modal):
    """Modal base with friendly error handling (modals are plain V2 modals)."""

    def __init__(self, *, title: str, ticket_id: Optional[int] = None, **kwargs):
        super().__init__(title=title, timeout=None, **kwargs)
        self.ticket_id = ticket_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, *args, **kwargs) -> None:
        log.exception(
            "Modal error in %s (ticket=%s)", type(self).__name__, self.ticket_id, exc_info=error
        )
        await respond_view_error(interaction, error)


# ---------------------------------------------------------------------------
# panel data (content) + Components V2 renderers
# ---------------------------------------------------------------------------

@dataclass
class PanelData:
    """Structured panel content, rendered into a borderless container."""

    title: str
    body: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: Optional[str] = None
    avatar_url: Optional[str] = None


def panel_container(data: PanelData) -> discord.ui.Container:
    """Heading + body/fields block (avatar shown as section thumbnail)."""
    container = discord.ui.Container()  # accent_color=None -> no accent border
    container.add_item(discord.ui.TextDisplay(f"## {data.title}"))

    blocks: list[str] = []
    if data.body:
        blocks.append(data.body)
    if data.fields:
        blocks.append("\n".join(f"**{name}:** {value}" for name, value in data.fields))
    text = "\n\n".join(blocks)
    if text:
        if data.avatar_url:
            container.add_item(
                discord.ui.Section(text, accessory=discord.ui.Thumbnail(data.avatar_url))
            )
        else:
            container.add_item(discord.ui.TextDisplay(text))
    return container


def panel_footer(container: discord.ui.Container, data: PanelData) -> None:
    """Append the divider + footer line. Call after adding controls."""
    if data.footer:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(data.footer))


def plain_panel_view(data: PanelData) -> discord.ui.LayoutView:
    """Container-only message (all controls removed/disabled states)."""
    container = panel_container(data)
    panel_footer(container, data)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# panel data builders
# ---------------------------------------------------------------------------

def build_intro_data(ticket: Ticket) -> PanelData:
    return PanelData(
        title=with_emoji("mm", "MM Escrow"),
        body="A secure middleman ticket for your trade.\n"
        "Add the person you're trading with, then both traders pick a role.",
        fields=[
            ("Ticket", f"**{ticket.label}**"),
            ("Creator", f"<@{ticket.creator_id}>"),
            ("Status", status_label(ticket.status)),
        ],
        footer=f"Ticket ID {ticket.id}",
    )


def build_status_data(
    ticket: Ticket,
    traders: Sequence[Trader],
    note: Optional[str] = None,
) -> PanelData:
    role_by_user = {t.user_id: t for t in traders}
    creator_trader = role_by_user.get(ticket.creator_id)
    partner_trader = role_by_user.get(ticket.partner_id) if ticket.partner_id else None

    if note:
        default_desc = note
    elif ticket.status in (TicketStatus.WAITING_FOR_CONFIRMATION, TicketStatus.CONFIRMED):
        default_desc = "Waiting for both traders to confirm the trade."
    elif ticket.status == TicketStatus.WAITING_FOR_MM:
        default_desc = "Waiting for a middleman to claim this ticket."
    elif ticket.status in TicketStatus.CLAIMED and ticket.claimed_mm_id:
        default_desc = f"Middleman <@{ticket.claimed_mm_id}> is handling this trade."
    else:
        default_desc = "Select your trading role below. Each trader picks exactly one."

    fields: list[tuple[str, str]] = [
        (
            "Trader 1",
            f"<@{ticket.creator_id}>\nRole: {role_label(creator_trader.trade_role if creator_trader else None)}",
        )
    ]
    if ticket.partner_id:
        partner_role = role_label(partner_trader.trade_role if partner_trader else None)
        fields.append(("Trader 2", f"<@{ticket.partner_id}>\nRole: {partner_role}"))
    else:
        fields.append(("Trader 2", "Not added yet\nRole: —"))

    waiting = "Waiting for both traders to pick their roles."
    if ticket.partner_id and creator_trader and creator_trader.trade_role and (
        partner_trader and partner_trader.trade_role
    ):
        waiting = "Both roles selected — confirmation follows."
    elif ticket.partner_id and not (creator_trader and creator_trader.trade_role):
        waiting = "Waiting for the creator to pick a role."
    elif ticket.partner_id:
        waiting = "Waiting for the trading partner to pick a role."
    fields.append(("Status", f"{status_label(ticket.status)}\n{waiting}"))

    return PanelData(
        title=with_emoji("ticket", ticket.label),
        body=truncate(default_desc, 900),
        fields=fields,
        footer=f"Ticket ID {ticket.id}",
    )


def build_confirmation_data(
    ticket: Ticket,
    traders: Sequence[Trader],
    state: str = "pending",
    detail: Optional[str] = None,
) -> PanelData:
    role_by_user = {t.user_id: t for t in traders}
    buyer_id = seller_id = None
    for trader in traders:
        if trader.trade_role == "BUYER":
            buyer_id = trader.user_id
        elif trader.trade_role == "SELLER":
            seller_id = trader.user_id

    buyer = role_by_user.get(buyer_id) if buyer_id else None
    seller = role_by_user.get(seller_id) if seller_id else None

    if state == "confirmed":
        description = "Both traders confirmed the trade. A middleman can now claim this ticket."
    elif state == "declined":
        description = "Confirmation was declined and has been reset."
    elif state == "expired":
        description = "Confirmation expired and has been reset."
    else:
        description = "Both traders must confirm the trade details before an MM can claim this ticket."
    if detail:
        description = f"{description}\n{truncate(detail, 400)}"

    def _state(trader) -> str:
        if trader is None:
            return "Waiting"
        return "Confirmed" if trader.confirmed else "Waiting"

    fields = [
        ("Buyer", f"<@{buyer_id}>" if buyer_id else "—"),
        ("Seller", f"<@{seller_id}>" if seller_id else "—"),
        ("Confirmation", f"Buyer: {_state(buyer)}\nSeller: {_state(seller)}"),
    ]
    if state == "pending" and ticket.confirm_expires_at:
        footer = f"Expires {format_ts(ticket.confirm_expires_at, 'R')}"
    else:
        footer = f"Ticket {ticket.label} • ID {ticket.id}"

    return PanelData(
        title=with_emoji("confirm", "MM Trade Confirmation"),
        body=description,
        fields=fields,
        footer=footer,
    )


def build_mm_request_data(ticket: Ticket) -> PanelData:
    return PanelData(
        title=with_emoji("money", "MM Required"),
        body="The traders have both confirmed their trade.\n"
        "A middleman is needed to handle this trade.",
        fields=[
            ("Ticket", f"**{ticket.label}**"),
            ("Buyer / Seller", _buyer_seller_line(ticket)),
            ("Status", status_label(ticket.status)),
        ],
        footer=f"Ticket ID {ticket.id}",
    )


def _buyer_seller_line(ticket: Ticket) -> str:
    return f"<@{ticket.creator_id}> / <@{ticket.partner_id}>" if ticket.partner_id else "—"


def build_assigned_data(ticket: Ticket, mm_id: int, avatar_url: Optional[str]) -> PanelData:
    return PanelData(
        title=with_emoji("mm", "Middleman Assigned"),
        body="Your middleman has claimed this ticket.",
        fields=[
            ("MM", f"<@{mm_id}>"),
            ("ID", str(mm_id)),
            ("Ticket", ticket.label),
            ("Claimed", format_ts(ticket.claimed_at, "F") if ticket.claimed_at else "—"),
            ("Status", status_label(ticket.status)),
        ],
        footer=f"Ticket ID {ticket.id}",
        avatar_url=avatar_url,
    )


def build_profile_data(
    ticket: Ticket, mm_id: int, username: str, avatar_url: Optional[str]
) -> PanelData:
    fields = [
        ("Username", f"<@{mm_id}>"),
        ("User ID", str(mm_id)),
        ("Ticket", ticket.label),
        ("Claimed", format_ts(ticket.claimed_at, "F") if ticket.claimed_at else "—"),
        ("Status", status_label(ticket.status)),
    ]
    if username:
        fields.append(("Account", truncate(username, 100)))
    return PanelData(
        title=with_emoji("profile", "Middleman Information"),
        body="Details of the middleman assigned to this ticket.",
        fields=fields,
        footer=f"Ticket ID {ticket.id}",
        avatar_url=avatar_url,
    )


def build_terminal_data(ticket: Ticket) -> PanelData:
    label = status_label(ticket.status)
    fields: list[tuple[str, str]] = [("Ticket", ticket.label)]
    if ticket.closed_at:
        fields.append(("Closed", format_ts(ticket.closed_at, "F")))
    if ticket.cancel_reason:
        fields.append(("Reason", truncate(ticket.cancel_reason, 200)))
    if ticket.claimed_mm_id:
        fields.append(("Middleman", f"<@{ticket.claimed_mm_id}>"))
    return PanelData(
        title=with_emoji("lock", f"Ticket {label}"),
        body=f"This ticket has been marked **{label.lower()}**. "
        "Controls are disabled; the audit history is preserved.",
        fields=fields,
        footer=f"Ticket ID {ticket.id}",
    )


# ---------------------------------------------------------------------------
# stage helpers
# ---------------------------------------------------------------------------

def stage_for_status(status: str) -> str:
    """Which TicketPanelView stage matches a ticket state."""
    if status in (TicketStatus.CREATED, TicketStatus.WAITING_FOR_PARTNER):
        return "intro"
    if status in (
        TicketStatus.PARTNER_ADDED,
        TicketStatus.WAITING_FOR_ROLES,
        TicketStatus.ROLES_SELECTED,
    ):
        return "roles"
    return "flat"


# ---------------------------------------------------------------------------
# panel message helpers
# ---------------------------------------------------------------------------

async def get_panel_message(guild: discord.Guild, ticket: Ticket, field_name: str) -> Optional[discord.Message]:
    """Fetch a stored panel message (partial — one small GET)."""
    if not ticket.channel_id:
        return None
    message_id = getattr(ticket, field_name, None)
    if not message_id:
        return None
    channel = guild.get_channel(ticket.channel_id)
    if channel is None or not hasattr(channel, "get_partial_message"):
        return None
    try:
        return await channel.get_partial_message(message_id).fetch()
    except discord.NotFound:
        return None
    except discord.HTTPException:
        log.warning("Could not fetch %s for ticket %s", field_name, ticket.label)
        return None


async def send_and_store(
    db,
    channel,
    ticket: Ticket,
    field_name: str,
    *,
    content: Optional[str] = None,
    view: Optional[discord.ui.LayoutView] = None,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
) -> Optional[discord.Message]:
    """Send a Components V2 panel message and persist its id for recovery."""
    message = await safe_send(
        channel,
        content=content,
        view=view,
        allowed_mentions=allowed_mentions,
    )
    if message is not None:
        try:
            await db.set_message_id(ticket.id, field_name, message.id)
        except Exception:
            log.exception("Could not store %s for ticket %s", field_name, ticket.label)
    return message


async def edit_panel(
    guild: discord.Guild,
    ticket: Ticket,
    field_name: str,
    *,
    build: Callable[[], discord.ui.LayoutView],
    content: "Optional[str | discord.utils.Missing]" = discord.utils.MISSING,
    allowed_mentions: "Optional[discord.AllowedMentions | discord.utils.Missing]" = discord.utils.MISSING,
) -> bool:
    """Edit a stored panel in place. False when it is gone.

    Legacy embed-only messages cannot gain components (the ``components_v2``
    flag is immutable), so they are deleted and the caller's resend path
    (or restart recovery) rebuilds them as V2.
    """
    message = await get_panel_message(guild, ticket, field_name)
    if message is None:
        return False
    if not message.flags.components_v2:
        log.info(
            "Replacing legacy embed panel %s for ticket %s with Components V2",
            field_name,
            ticket.label,
        )
        try:
            await message.delete()
        except discord.HTTPException:
            log.warning("Could not delete legacy panel %s for %s", field_name, ticket.label)
        return False
    kwargs: dict = {"view": build(), "embed": None}
    if content is not discord.utils.MISSING:
        kwargs["content"] = content
    if allowed_mentions is not discord.utils.MISSING:
        kwargs["allowed_mentions"] = allowed_mentions
    return await safe_edit(message, **kwargs)


async def panel_status(
    guild: discord.Guild, ticket: Ticket, field_name: str
) -> Optional[bool]:
    """True = exists, False = definitively gone/never sent, None = unknown (API error)."""
    if not getattr(ticket, field_name, None):
        return False
    channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
    if channel is None or not hasattr(channel, "get_partial_message"):
        return None
    try:
        await channel.get_partial_message(getattr(ticket, field_name)).fetch()
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException:
        return None


async def refresh_status_panel(
    bot,
    ticket: Ticket,
    traders: Optional[Sequence[Trader]] = None,
    note: Optional[str] = None,
) -> bool:
    """Re-render the main ticket panel (Msg1) for the ticket's current state."""
    from views.ticket_views import TicketPanelView  # local import: avoid cycles

    guild = bot.get_guild(ticket.guild_id)
    if guild is None:
        return False
    if traders is None:
        traders = await bot.db.get_traders(ticket.id)

    stage = stage_for_status(ticket.status)
    if ticket.status in (TicketStatus.CREATED, TicketStatus.WAITING_FOR_PARTNER):
        data = build_intro_data(ticket)
    else:
        data = build_status_data(ticket, traders, note=note)

    ok = await edit_panel(
        guild,
        ticket,
        "status_message_id",
        build=lambda: TicketPanelView(ticket.id, stage=stage, data=data),
    )
    if not ok and ticket.channel_id:
        channel = guild.get_channel(ticket.channel_id)
        if channel is not None:
            message = await send_and_store(
                bot.db,
                channel,
                ticket,
                "status_message_id",
                view=TicketPanelView(ticket.id, stage=stage, data=data),
            )
            return message is not None
    return ok


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------

async def generate_ticket_transcript(bot: discord.Client, ticket: Ticket) -> None:
    """Build, save and (optionally) deliver a transcript. Never raises."""
    if not config.TRANSCRIPT_ENABLED:
        return
    try:
        db = bot.db
        fresh = await db.get_ticket(ticket.id) or ticket
        traders = await db.get_traders(ticket.id)
        claims = await db.get_claims(ticket.id)
        events = await db.get_events(ticket.id, limit=500)
        reports = await _load_reports(db, ticket.id)

        message_lines: Optional[list[str]] = None
        if config.TRANSCRIPT_INCLUDE_MESSAGES and fresh.channel_id:
            channel = bot.get_channel(fresh.channel_id)
            if channel is not None:
                message_lines = await collect_channel_messages(
                    channel, config.TRANSCRIPT_MESSAGE_LIMIT
                )

        text = build_transcript(fresh, traders, claims, events, reports, bot, message_lines)
        await deliver_transcript(bot, fresh, text)
    except Exception:
        log.exception("Transcript generation failed for %s", ticket.label)


async def _load_reports(db, ticket_id: int):
    from database.models import Report  # local import keeps module load light

    try:
        rows = await db._fetchall("SELECT * FROM mm_reports WHERE ticket_id = ? ORDER BY id", (ticket_id,))
        return [Report.from_row(r) for r in rows]
    except Exception:
        log.exception("Could not load reports for transcript")
        return []
