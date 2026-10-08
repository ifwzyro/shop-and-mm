"""Interaction validation, safe messaging helpers and app-command checks.

Every helper here treats Discord input as untrusted and never exposes
tracebacks to users.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

import discord
from discord import app_commands

import config
from database.models import Ticket
from utils import permissions

log = logging.getLogger("mm.checks")

_MENTION_RE = re.compile(r"^(?:<@!?)?(\d{5,30})>?$")
_DIGITS_RE = re.compile(r"^\d{5,30}$")


# ---------------------------------------------------------------------------
# input parsing
# ---------------------------------------------------------------------------

def parse_user_id(raw: str) -> Optional[int]:
    """Extract a user ID from a raw mention or ID string."""
    if not raw:
        return None
    text = raw.strip().strip("@")
    match = _MENTION_RE.match(text) or _DIGITS_RE.match(text)
    if not match:
        return None
    return int(match.group(1) if _MENTION_RE.match(text) else match.group(0))


def validate_role_pair(
    roles: Sequence[Optional[str]], allow_same: bool = False
) -> tuple[str, Optional[str]]:
    """Validate the buyer/seller pair.

    Returns ``(state, message)`` where state is one of:
    ``incomplete`` (not both picked yet), ``valid``, or ``conflict``.
    """
    if len(roles) < 2 or any(r not in ("BUYER", "SELLER") for r in roles):
        return "incomplete", None
    if roles[0] == roles[1] and not allow_same:
        label = "Buyer" if roles[0] == "BUYER" else "Seller"
        other = "Seller" if roles[0] == "BUYER" else "Buyer"
        return "conflict", (
            f"Both traders cannot be {label}. One trader must be {other}."
        )
    return "valid", None


# ---------------------------------------------------------------------------
# safe message helpers (rate-limit friendly, never raise to users)
# ---------------------------------------------------------------------------

async def send_interaction(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional["discord.ui.LayoutView"] = None,
    ephemeral: bool = True,
    delete_after: Optional[float] = None,
) -> bool:
    """Respond (or follow up) exactly once, swallowing Discord errors."""
    kwargs: dict = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if delete_after is not None and delete_after > 0:
        kwargs["delete_after"] = delete_after
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
        return True
    except discord.HTTPException:
        log.exception("Failed to respond to interaction %s", interaction.id)
        return False


async def safe_edit(message: Optional[discord.Message], **kwargs) -> bool:
    """Edit a bot message; returns False instead of raising."""
    if message is None:
        return False
    try:
        await message.edit(**kwargs)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException:
        log.exception("Failed to edit message %s", getattr(message, "id", "?"))
        return False


async def safe_send(channel, **kwargs):
    """Send to a channel; returns the message or None instead of raising."""
    try:
        return await channel.send(**kwargs)
    except discord.Forbidden:
        log.warning("Missing permissions to send in channel %s", getattr(channel, "id", "?"))
        return None
    except discord.HTTPException:
        log.exception("Failed to send to channel %s", getattr(channel, "id", "?"))
        return None


async def send_dm(user: discord.abc.User, **kwargs) -> bool:
    """Send a DM; False when DMs are closed or the API fails."""
    try:
        await user.send(**kwargs)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        log.exception("Failed to DM user %s", getattr(user, "id", "?"))
        return False


# ---------------------------------------------------------------------------
# shared validation used by views
# ---------------------------------------------------------------------------

async def load_ticket_for_view(
    interaction: discord.Interaction,
    ticket_id: int,
    *,
    require_channel: bool = True,
    statuses: Optional[Sequence[str]] = None,
) -> tuple[Optional[Ticket], Optional[str]]:
    """Resolve and validate the ticket behind a component interaction.

    Returns ``(ticket, None)`` on success or ``(None, friendly_error)``.
    Verifies guild, channel binding and stored state — never the button's
    visible state.
    """
    db = getattr(interaction.client, "db", None)
    if db is None:
        return None, "The database is not available right now. Please try again later."

    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        return None, "This ticket no longer exists."

    if interaction.guild_id is None or ticket.guild_id != interaction.guild_id:
        return None, "This action can only be used inside the server."

    if require_channel and interaction.channel_id and ticket.channel_id != interaction.channel_id:
        return None, "This button belongs to a different ticket."

    if statuses is not None and ticket.status not in statuses:
        return None, (
            "This action isn't available for this ticket right now "
            f"(current state: {ticket.status.replace('_', ' ').title()})."
        )
    return ticket, None


async def is_ticket_trader(db, ticket: Ticket, user_id: int) -> bool:
    trader = await db.get_trader(ticket.id, user_id)
    return trader is not None or user_id in (ticket.creator_id, ticket.partner_id)


# ---------------------------------------------------------------------------
# ticket reference resolution (slash commands, staff recovery)
# ---------------------------------------------------------------------------

async def resolve_ticket_ref(
    db,
    guild: discord.Guild,
    raw: Optional[str],
    *,
    fallback_channel_id: Optional[int] = None,
) -> Optional[Ticket]:
    """Resolve ``MM-0042`` / ``42`` / ``<#channel>`` / numeric id to a ticket."""
    if raw:
        text = raw.strip()
        channel_match = re.match(r"^<#(\d+)>$", text)
        if channel_match:
            ticket = await db.get_ticket_by_channel(int(channel_match.group(1)))
            if ticket:
                return ticket
        prefix = config.TICKET_PREFIX.upper()
        label_match = re.match(rf"^{re.escape(prefix)}-?(\d+)$", text, re.IGNORECASE)
        if label_match:
            ticket = await db.get_ticket_by_number(guild.id, int(label_match.group(1)))
            if ticket:
                return ticket
        if text.isdigit():
            number = int(text)
            ticket = await db.get_ticket_by_number(guild.id, number)
            if ticket:
                return ticket
            ticket = await db.get_ticket(number)
            if ticket and ticket.guild_id == guild.id:
                return ticket
        return None
    if fallback_channel_id:
        return await db.get_ticket_by_channel(fallback_channel_id)
    return None


# ---------------------------------------------------------------------------
# app command predicates
# ---------------------------------------------------------------------------

def require_guild():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        return True

    return app_commands.check(predicate)


def require_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if not permissions.is_staff(interaction.user):
            raise app_commands.CheckFailure("You need a staff role to use this command.")
        return True

    return app_commands.check(predicate)


def require_mm():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        member = interaction.user
        if not (permissions.can_claim_mm(member) or permissions.is_staff(member)):
            raise app_commands.CheckFailure("You need the middleman role to use this command.")
        return True

    return app_commands.check(predicate)
