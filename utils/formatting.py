"""Embed builders, labels, audit formatting and transcript generation."""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import discord

import config
from database.models import Event, Report, Ticket, TicketStatus, Trader
from utils.emojis import with_emoji
from utils.time import clock, format_ts, now, short_date

log = logging.getLogger("mm.formatting")

# ---------------------------------------------------------------------------
# Components V2 notices (ephemeral / one-shot messages)
# ---------------------------------------------------------------------------

_NOTICE_KEYS = {
    "info": None,
    "success": "success",
    "error": "error",
    "warning": "warning",
}


def notice(
    title: str,
    description: Optional[str] = None,
    *,
    kind: str = "info",
) -> "discord.ui.LayoutView":
    """Compact Components V2 message: heading + optional body, no accent bar.

    Used for every ephemeral command response and inline error/confirmation.
    """
    emoji_key = _NOTICE_KEYS.get(kind)
    heading = with_emoji(emoji_key, title) if emoji_key else title
    container = discord.ui.Container()  # accent_color=None -> no accent border
    container.add_item(discord.ui.TextDisplay(f"## {heading}"))
    if description:
        container.add_item(discord.ui.TextDisplay(description))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def success_notice(title: str, description: Optional[str] = None) -> "discord.ui.LayoutView":
    return notice(title, description, kind="success")


def error_notice(title: str, description: Optional[str] = None) -> "discord.ui.LayoutView":
    return notice(title, description, kind="error")


def warning_notice(title: str, description: Optional[str] = None) -> "discord.ui.LayoutView":
    return notice(title, description, kind="warning")


def link_notice(text: str, label: str, url: str) -> "discord.ui.LayoutView":
    """One-line Components V2 message carrying a single link button."""
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay(text))
    container.add_item(
        discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link)
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    TicketStatus.CREATED: "Created",
    TicketStatus.WAITING_FOR_PARTNER: "Waiting for trading partner",
    TicketStatus.PARTNER_ADDED: "Partner added",
    TicketStatus.WAITING_FOR_ROLES: "Waiting for roles",
    TicketStatus.ROLES_SELECTED: "Roles selected",
    TicketStatus.WAITING_FOR_CONFIRMATION: "Waiting for confirmations",
    TicketStatus.CONFIRMED: "Confirmed",
    TicketStatus.WAITING_FOR_MM: "Waiting for a middleman",
    TicketStatus.MM_CLAIMED: "Middleman assigned",
    TicketStatus.IN_PROGRESS: "Trade in progress",
    TicketStatus.COMPLETED: "Completed",
    TicketStatus.CANCELLED: "Cancelled",
    TicketStatus.DISPUTED: "Disputed",
    TicketStatus.CLOSED: "Closed",
}


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status.replace("_", " ").title())


def role_label(role: Optional[str]) -> str:
    if role == "BUYER":
        return "Buyer"
    if role == "SELLER":
        return "Seller"
    return "Not selected"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\u2026"


def jump_link(guild_id: int, channel_id: Optional[int], message_id: Optional[int]) -> Optional[str]:
    if not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# ---------------------------------------------------------------------------
# audit / timeline formatting
# ---------------------------------------------------------------------------

def describe_event(event: Event, ticket_label: str) -> str:
    """Human line for one audit event (actor included)."""
    actor = f"User {event.actor_id}" if event.actor_id else "System"
    meta = event.metadata or {}
    et = event.event_type

    if et == "TICKET_CREATED":
        return f"{actor} created the ticket"
    if et == "PARTNER_ADDED":
        return f"{actor} added trading partner {meta.get('partner_id')}"
    if et == "ROLE_SELECTED":
        return f"{actor} selected {role_label(meta.get('role'))}"
    if et == "ROLE_CHANGED":
        return f"{actor} changed role to {role_label(meta.get('role'))}"
    if et == "CONFIRMATION_STARTED":
        return "Confirmation started"
    if et == "TRADER_CONFIRMED":
        extra = " — both traders confirmed" if meta.get("both") else ""
        return f"{actor} confirmed the trade ({role_label(meta.get('role'))}){extra}"
    if et == "TRADER_DECLINED":
        reason = meta.get("reason")
        suffix = f" — reason: {reason}" if reason else ""
        return f"{actor} declined the confirmation{suffix}"
    if et == "CONFIRMATION_EXPIRED":
        return "Confirmation expired and was reset"
    if et == "MM_REQUESTED":
        return "Both traders confirmed — middleman requested"
    if et == "MM_CLAIMED":
        via = " (staff force claim)" if meta.get("via") == "force" else ""
        mm = meta.get("mm_id") or (event.actor_id if meta.get("via") == "force" else event.actor_id)
        return f"MM {mm} claimed the ticket{via}"
    if et == "MM_RELEASED":
        return f"MM {meta.get('released_mm_id')} released the ticket"
    if et == "REPORT_CREATED":
        return f"{actor} submitted a report ({meta.get('category')})"
    if et == "REPORT_STATUS_CHANGED":
        return f"{actor} set report #{meta.get('report_id')} to {meta.get('status')}"
    if et == "TICKET_COMPLETED":
        return f"{actor} completed the trade"
    if et == "TICKET_CANCELLED":
        reason = meta.get("reason")
        suffix = f" — reason: {reason}" if reason else ""
        return f"{actor} cancelled the ticket{suffix}"
    if et == "TICKET_CLOSED":
        reason = meta.get("reason")
        suffix = f" — reason: {reason}" if reason else ""
        return f"{actor} closed the ticket{suffix}"
    if et == "TIMEOUT_WARNING":
        return f"Timeout warning sent ({meta.get('stage')})"
    if et == "TICKET_FLAGGED":
        return "Ticket flagged for review"
    if et == "CHANNEL_MISSING":
        return "Ticket channel missing — flagged for recovery"
    if et == "MM_ROLE_LOST":
        return f"Assigned MM {meta.get('mm_id')} no longer has the MM role"
    if et == "TICKET_ARCHIVED":
        return f"Ticket archived ({meta.get('mode')})"
    return et.replace("_", " ").title()


def format_timeline(events: Iterable[Event], ticket_label: str) -> str:
    """Audit lines in the documented format:

    ``[17:42:11]`` / ``MM-0042`` / ``User 123 selected Buyer``
    """
    lines: list[str] = []
    for event in events:
        lines.append(f"[{clock(event.created_at)}]\n{ticket_label}\n{describe_event(event, ticket_label)}")
    return "\n".join(lines) if lines else "No events recorded."


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------

def _user_str(user_id: Optional[int], bot: Optional[discord.Client] = None) -> str:
    if not user_id:
        return "—"
    if bot is not None:
        user = bot.get_user(user_id)
        if user is not None:
            return f"{user} ({user_id})"
    return str(user_id)


def build_transcript(
    ticket: Ticket,
    traders: Sequence[Trader],
    claims,
    events: Sequence[Event],
    reports: Sequence[Report],
    bot: Optional[discord.Client] = None,
    message_lines: Optional[Sequence[str]] = None,
) -> str:
    """Plain-text transcript of ticket state, participants and timeline."""
    width = 64
    out: list[str] = []
    out.append("=" * width)
    out.append(f"MM ESCROW TICKET — {ticket.label}")
    out.append("=" * width)
    out.append(f"Status:        {status_label(ticket.status)}")
    out.append(f"Ticket ID:     {ticket.id}")
    out.append(f"Channel ID:    {ticket.channel_id or '—'}")
    out.append(f"Created:       {short_date(ticket.created_at)} UTC ({format_ts(ticket.created_at, 'R')})")
    out.append(f"Closed:        {short_date(ticket.closed_at)} UTC" if ticket.closed_at else "Closed:        —")
    out.append("")

    out.append("PARTICIPANTS")
    out.append("-" * width)
    out.append(f"Creator:  {_user_str(ticket.creator_id, bot)}")
    out.append(f"Partner:  {_user_str(ticket.partner_id, bot)}")
    for trader in traders:
        confirmed = "confirmed" if trader.confirmed else "not confirmed"
        when = f" at {short_date(trader.confirmed_at)} UTC" if trader.confirmed_at else ""
        out.append(
            f"  {_user_str(trader.user_id, bot)} — {role_label(trader.trade_role)} ({confirmed}{when})"
        )
    out.append("")

    out.append("MIDDLEMAN")
    out.append("-" * width)
    if ticket.claimed_mm_id:
        claimed = f"{short_date(ticket.claimed_at)} UTC" if ticket.claimed_at else "—"
        out.append(f"Assigned: {_user_str(ticket.claimed_mm_id, bot)}")
        out.append(f"Claimed:  {claimed}")
    else:
        out.append("No middleman assigned.")
    if claims:
        out.append("Claim history:")
        for claim in claims:
            released = short_date(claim.released_at) if claim.released_at else "active"
            out.append(
                f"  {_user_str(claim.mm_id, bot)} — claimed {short_date(claim.claimed_at)} UTC, released: {released}"
            )
    out.append("")

    if reports:
        out.append("REPORTS")
        out.append("-" * width)
        for report in reports:
            out.append(
                f"  #{report.id} by {_user_str(report.reporter_id, bot)} — {report.category} [{report.status}]"
            )
        out.append("")

    out.append("TIMELINE")
    out.append("-" * width)
    out.append(format_timeline(events, ticket.label))
    out.append("")

    if message_lines:
        out.append("MESSAGES")
        out.append("-" * width)
        out.extend(message_lines)

    out.append("=" * width)
    out.append(f"Generated {short_date(now())} UTC")
    return "\n".join(out)


async def collect_channel_messages(channel, limit: int) -> list[str]:
    """Best-effort message dump, only when TRANSCRIPT_INCLUDE_MESSAGES is on."""
    lines: list[str] = []
    try:
        async for message in channel.history(limit=limit, oldest_first=True):
            if message.author.bot and message.author.id == (channel.guild.me.id if channel.guild else 0):
                author = "MM Bot"
            else:
                author = str(message.author)
            stamp = clock(int(message.created_at.timestamp()))
            body = message.content or (f"[embed: {message.embeds[0].title}]" if message.embeds else "[component]")
            lines.append(f"[{stamp}] {author}: {body}")
    except discord.HTTPException:
        log.exception("Failed to collect message history for transcript")
    return lines


async def deliver_transcript(bot: discord.Client, ticket: Ticket, text: str) -> None:
    """Save transcript to disk and optionally deliver it. Never raises."""
    if not config.TRANSCRIPT_ENABLED:
        return
    path = None
    try:
        save_dir = config.TRANSCRIPT_SAVE_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"{ticket.label}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError:
        log.exception("Could not save transcript for %s", ticket.label)
        path = None

    if config.TRANSCRIPT_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.TRANSCRIPT_CHANNEL_ID)
            if channel is None:
                channel = await bot.fetch_channel(config.TRANSCRIPT_CHANNEL_ID)
            file = discord.File(path, filename=f"{ticket.label}.txt") if path else None
            await channel.send(
                content=f"Transcript for {ticket.label}",
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Could not deliver transcript for %s", ticket.label)
