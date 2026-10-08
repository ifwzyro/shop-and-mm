"""Permission helpers. All role IDs come from ``config`` — never hard-coded."""

from __future__ import annotations

from typing import Optional

import discord

import config


def _has_role(member: Optional[discord.Member], role_id: int) -> bool:
    if member is None or not role_id:
        return False
    return any(role.id == role_id for role in member.roles)


def is_mm(member: Optional[discord.Member]) -> bool:
    """Base MM role."""
    return _has_role(member, config.MM_ROLE_ID)


def is_mm_admin(member: Optional[discord.Member]) -> bool:
    """Senior MM admin role."""
    return _has_role(member, config.MM_ADMIN_ROLE_ID)


def is_staff(member: Optional[discord.Member]) -> bool:
    """Staff, MM admin, or a guild administrator."""
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    return _has_role(member, config.STAFF_ROLE_ID) or _has_role(member, config.MM_ADMIN_ROLE_ID)


def can_claim_mm(member: Optional[discord.Member]) -> bool:
    """Whether this member may claim MM tickets via the claim button."""
    if member is None:
        return False
    if _has_role(member, config.MM_ROLE_ID):
        return True
    return config.ALLOW_MM_ADMIN_CLAIM and is_mm_admin(member)


def can_moderate_reports(member: Optional[discord.Member]) -> bool:
    """Acknowledge / resolve / escalate reports."""
    return is_staff(member)


def can_create_tickets(member: Optional[discord.Member]) -> bool:
    """Optional gate on who may open MM tickets."""
    if member is None:
        return False
    if not config.REQUIRED_ROLE_ID:
        return True
    if member.guild_permissions.administrator:
        return True
    return _has_role(member, config.REQUIRED_ROLE_ID) or is_staff(member)


def bot_missing_channel_perms(channel: discord.abc.GuildChannel) -> list[str]:
    """Names of permissions the bot is missing in *channel* (for startup checks)."""
    perms = channel.permissions_for(channel.guild.me) if isinstance(channel, discord.TextChannel) else None
    if perms is None:
        return ["unknown"]
    needed = {
        "view_channel": "View Channel",
        "send_messages": "Send Messages",
        "embed_links": "Embed Links",
        "attach_files": "Attach Files",
        "read_message_history": "Read Message History",
    }
    return [label for attr, label in needed.items() if not getattr(perms, attr, False)]
