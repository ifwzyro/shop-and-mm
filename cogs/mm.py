"""Core MM cog: command group, setup/status commands, recovery + monitor."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database.models import Ticket, TicketStatus
from utils import permissions
from utils.checks import require_guild, require_staff, send_interaction
from utils.emojis import with_emoji
from utils.formatting import notice
from utils.time import format_ts, now
from views.base import PanelData, plain_panel_view

log = logging.getLogger("mm.cogs.mm")

# The single /mm command group. It is registered on the command tree in
# main.py after all cogs are loaded, so every cog can add subcommands.
mm_group = app_commands.Group(name="mm", description="Middleman escrow commands")


def build_ticket_overview(ticket: Ticket, traders=None) -> PanelData:
    """Compact status overview used by /mm status and /mm ticketinfo."""
    fields: list[tuple[str, str]] = [
        ("Creator", f"<@{ticket.creator_id}>"),
        (
            "Partner",
            f"<@{ticket.partner_id}>" if ticket.partner_id else "Not added yet",
        ),
    ]
    if traders:
        buyer = seller = "—"
        buyer_c = seller_c = "Waiting"
        for trader in traders:
            if trader.trade_role == "BUYER":
                buyer = f"<@{trader.user_id}>"
                buyer_c = "Confirmed" if trader.confirmed else "Waiting"
            elif trader.trade_role == "SELLER":
                seller = f"<@{trader.user_id}>"
                seller_c = "Confirmed" if trader.confirmed else "Waiting"
        fields.append(("Buyer", f"{buyer}\n{buyer_c}"))
        fields.append(("Seller", f"{seller}\n{seller_c}"))
    if ticket.claimed_mm_id:
        fields.append(
            (
                "Middleman",
                f"<@{ticket.claimed_mm_id}>\nClaimed {format_ts(ticket.claimed_at, 'R')}",
            )
        )
    fields.extend(
        (
            ("Status", ticket.status.replace("_", " ").title()),
            ("Created", format_ts(ticket.created_at, "R")),
        )
    )
    if ticket.flagged:
        fields.append(("Review", "Flagged for staff review"))
    return PanelData(
        title=with_emoji("ticket", f"{ticket.label} — {ticket.status.replace('_', ' ').title()}"),
        body=f"Channel: <#{ticket.channel_id}>" if ticket.channel_id else "Channel: unavailable",
        fields=fields,
        footer=f"Ticket ID {ticket.id}",
    )


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------

@mm_group.command(name="setup", description="Post the MM Escrow ticket panel here (staff)")
@require_staff()
async def mm_setup(interaction: discord.Interaction) -> None:
    from views.mm_panel import SetupPanelView

    try:
        await interaction.response.send_message(
            view=SetupPanelView(), allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException:
        log.exception("Could not post setup panel in %s", interaction.channel_id)
        await send_interaction(
            interaction,
            content="I couldn't post the panel here — check my permissions and try again.",
            ephemeral=True,
        )


@mm_group.command(name="status", description="Show the status of your active MM ticket")
@require_guild()
async def mm_status(interaction: discord.Interaction) -> None:
    db = interaction.client.db
    ticket = await db.find_active_ticket_for_user(interaction.user.id)
    if ticket is None and interaction.channel_id:
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
    if ticket is None:
        await send_interaction(
            interaction,
            view=notice(
                "No active ticket",
                "You don't have an active MM ticket.\nUse **Create MM Ticket** on the panel to open one.",
            ),
            ephemeral=True,
        )
        return
    traders = await db.get_traders(ticket.id)
    await send_interaction(
        interaction,
        view=plain_panel_view(build_ticket_overview(ticket, traders)),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# cog: startup validation, recovery, monitor
# ---------------------------------------------------------------------------

class MMCog(commands.Cog):
    """Startup validation, restart recovery and the background monitor."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._initialized = False

    async def cog_load(self) -> None:
        self.monitor_loop.start()

    async def cog_unload(self) -> None:
        self.monitor_loop.cancel()

    # -- startup --------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        log.info("Bot ready as %s — running startup validation and recovery", self.bot.user)
        try:
            await self._validate_configuration()
            await self._recover_active_tickets()
        except Exception:
            log.exception("Startup recovery failed")
        finally:
            self.bot.startup_event.set()

        if config.SYNC_COMMANDS:
            try:
                synced = await self.bot.tree.sync()
                log.info("Synced %d application commands", len(synced))
                if config.GUILD_ID:
                    await self.bot.tree.sync(guild=discord.Object(id=config.GUILD_ID))
            except discord.HTTPException:
                log.exception("Command sync failed")

    async def _validate_configuration(self) -> None:
        """Warn about missing roles/channels — never crash on non-fatal issues."""
        if not config.REPORT_CHANNEL_ID:
            log.error(
                "REPORT_CHANNEL_ID is not configured — MM reports cannot be delivered!"
            )
        if not config.MM_ROLE_ID:
            log.error("MM_ROLE_ID is not configured — nobody can claim tickets normally.")

        for guild in self.bot.guilds:
            if config.GUILD_ID and guild.id != config.GUILD_ID:
                continue
            for role_key in ("MM_ROLE_ID", "STAFF_ROLE_ID", "MM_ADMIN_ROLE_ID", "REQUIRED_ROLE_ID"):
                role_id = getattr(config, role_key, 0)
                if role_id and guild.get_role(role_id) is None:
                    log.warning("Configured %s (%s) not found in guild %s", role_key, role_id, guild.id)

            if config.MM_CATEGORY_ID:
                category = guild.get_channel(config.MM_CATEGORY_ID)
                if category is not None and not isinstance(category, discord.CategoryChannel):
                    log.warning(
                        "MM_CATEGORY_ID (%s) is not a category channel in guild %s",
                        config.MM_CATEGORY_ID,
                        guild.id,
                    )
                    category = None
                if category is None:
                    log.warning("MM_CATEGORY_ID (%s) not found in guild %s", config.MM_CATEGORY_ID, guild.id)
                elif guild.me:
                    perms = category.permissions_for(guild.me)
                    if not perms.manage_channels or not perms.view_channel:
                        log.warning(
                            "Bot lacks Manage Channels/View Channel in MM category %s", config.MM_CATEGORY_ID
                        )

            if config.REPORT_CHANNEL_ID:
                channel = guild.get_channel(config.REPORT_CHANNEL_ID)
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(config.REPORT_CHANNEL_ID)
                    except discord.HTTPException:
                        channel = None
                if channel is None:
                    log.warning("REPORT_CHANNEL_ID (%s) not found in guild %s", config.REPORT_CHANNEL_ID, guild.id)
                elif guild.me:
                    perms = channel.permissions_for(guild.me)
                    missing = [
                        label
                        for attr, label in (
                            ("view_channel", "View Channel"),
                            ("send_messages", "Send Messages"),
                            ("embed_links", "Embed Links"),
                        )
                        if not getattr(perms, attr, False)
                    ]
                    if missing:
                        log.warning(
                            "Report channel %s missing permissions: %s",
                            config.REPORT_CHANNEL_ID,
                            ", ".join(missing),
                        )
            break  # validate the primary guild only

    async def _recover_active_tickets(self) -> None:
        """Restore views and panels for every active ticket (restart recovery)."""
        from views.mm_panel import restore_ticket_panels

        db = self.bot.db
        active = await db.list_active_tickets()
        if active:
            log.info("Recovering %d active ticket(s) after restart", len(active))
        for ticket in active:
            try:
                await restore_ticket_panels(self.bot, ticket)
            except Exception:
                log.exception("Recovery failed for ticket %s", ticket.label)

        # Refresh the flagged state of report views for open reports.
        from views import register_report_views

        try:
            register_report_views(self.bot, await db.list_open_reports())
        except Exception:
            log.exception("Could not register open report views")

    # -- monitor --------------------------------------------------------

    @tasks.loop(seconds=config.MONITOR_INTERVAL)
    async def monitor_loop(self) -> None:
        """Runs every MONITOR_INTERVAL seconds; waits for startup recovery first."""
        try:
            await self.bot.startup_event.wait()
            await self._monitor_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Monitor cycle failed")

    async def _monitor_cycle(self) -> None:
        db = self.bot.db
        stamp = now()
        for ticket in await db.list_active_tickets():
            try:
                await self._check_ticket(ticket, stamp)
            except Exception:
                log.exception("Monitor check failed for %s", ticket.label)

        if config.AUTO_ARCHIVE_DELAY > 0 and config.ARCHIVE_MODE != "none":
            deadline = stamp - config.AUTO_ARCHIVE_DELAY
            for ticket in await db.list_archivable(deadline):
                try:
                    await self._archive_ticket(ticket, stamp)
                except Exception:
                    log.exception("Archive failed for %s", ticket.label)

    async def _check_ticket(self, ticket: Ticket, stamp: int) -> None:
        db = self.bot.db
        guild = self.bot.get_guild(ticket.guild_id)

        # Channel vanished?
        if (
            ticket.is_active
            and not ticket.channel_missing
            and ticket.channel_id
            and guild is not None
            and guild.get_channel(ticket.channel_id) is None
        ):
            from views.mm_panel import _flag_channel_missing

            await _flag_channel_missing(db, ticket, guild)
            return

        status = ticket.status
        stage: Optional[str] = None
        deadline: Optional[int] = None

        if status == TicketStatus.WAITING_FOR_PARTNER:
            stage, deadline = "partner", ticket.created_at + config.PARTNER_TIMEOUT
        elif status in (TicketStatus.PARTNER_ADDED, TicketStatus.WAITING_FOR_ROLES):
            base = ticket.partner_added_at or ticket.stage_started_at
            stage, deadline = "roles", base + config.ROLE_TIMEOUT
        elif status == TicketStatus.ROLES_SELECTED:
            stage, deadline = "restart", ticket.stage_started_at + config.ROLE_TIMEOUT
        elif status == TicketStatus.WAITING_FOR_CONFIRMATION:
            if ticket.confirm_expires_at and stamp >= ticket.confirm_expires_at:
                from views.confirmation_views import expire_confirmation

                if await expire_confirmation(self.bot, ticket):
                    log.info("Confirmation expired for %s", ticket.label)
                return
        elif status == TicketStatus.CONFIRMED:
            # Crash recovery: finish the transition and send the MM request.
            if await db.begin_mm_request(ticket.id, stamp):
                ticket = await db.get_ticket(ticket.id) or ticket
                log.info("Recovered CONFIRMED ticket %s -> waiting for MM", ticket.label)
        elif status == TicketStatus.WAITING_FOR_MM:
            if ticket.mm_message_id is None:
                from views.confirmation_views import send_mm_request

                await send_mm_request(self.bot, ticket)
                ticket = await db.get_ticket(ticket.id) or ticket
            stage = "mm_claim"
            deadline = (ticket.mm_requested_at or ticket.stage_started_at) + config.MM_CLAIM_TIMEOUT
        elif status in TicketStatus.CLAIMED:
            await self._check_assigned_mm(ticket, guild)
            stage = "inactive"
            deadline = (ticket.last_activity_at or ticket.stage_started_at) + config.INACTIVE_TICKET_TIMEOUT

        if stage is None or deadline is None or stamp < deadline:
            return

        # One warning per stage, then flag for review.
        if not await db.mark_warned(ticket.id, stage, stamp):
            return
        await db.add_event(ticket.id, None, "TIMEOUT_WARNING", {"stage": stage}, stamp)
        await self._warn_users(ticket, stage, guild)
        if await db.flag_ticket(ticket.id, stamp):
            await db.add_event(ticket.id, None, "TICKET_FLAGGED", {"stage": stage}, stamp)
            await self._notify_staff(
                guild,
                f"Ticket **{ticket.label}** (ID {ticket.id}) was flagged for review "
                f"(stage: {stage}).",
            )

    async def _check_assigned_mm(self, ticket: Ticket, guild: Optional[discord.Guild]) -> None:
        """If the assigned MM lost the MM role or left, flag once — never auto-destroy."""
        if not ticket.claimed_mm_id or not config.MM_ROLE_ID or guild is None:
            return
        db = self.bot.db
        member = guild.get_member(ticket.claimed_mm_id)
        still_mm = False
        if member is not None:
            still_mm = permissions.is_mm(member) or permissions.is_mm_admin(member)
        if still_mm:
            return
        if await db.has_event(ticket.id, "MM_ROLE_LOST"):
            return
        await db.add_event(
            ticket.id, None, "MM_ROLE_LOST", {"mm_id": ticket.claimed_mm_id, "left": member is None}, now()
        )
        await db.flag_ticket(ticket.id, now())
        log.warning("Assigned MM %s lost their role on %s", ticket.claimed_mm_id, ticket.label)
        await self._notify_staff(
            guild,
            f"Assigned MM <@{ticket.claimed_mm_id}> no longer has the MM role on **{ticket.label}**. "
            f"Use `/mm release {ticket.label}` and `/mm forceclaim {ticket.label}` to recover.",
        )

    async def _warn_users(self, ticket: Ticket, stage: str, guild: Optional[discord.Guild]) -> None:
        if guild is None or not ticket.channel_id:
            return
        channel = guild.get_channel(ticket.channel_id)
        if channel is None:
            return
        from utils.checks import safe_send

        label = ticket.label
        if stage == "partner":
            content = (
                f"<@{ticket.creator_id}> — no trading partner has been added to **{label}** yet. "
                "Add one soon or this ticket will stay flagged for review."
            )
        elif stage in ("roles", "restart"):
            content = (
                f"<@{ticket.creator_id}> <@{ticket.partner_id}> — **{label}** is waiting on you. "
                "Select your roles and confirmation to continue."
            )
        elif stage == "mm_claim":
            content = (
                f"**{label}** still needs a middleman."
                + (f" <@&{config.MM_ROLE_ID}>" if config.MM_ROLE_ID else "")
            )
        else:  # inactive
            content = (
                f"<@{ticket.creator_id}> <@{ticket.partner_id}> — **{label}** has been inactive "
                "for a long time and is flagged for review."
            )
        await safe_send(
            channel,
            content=content,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=[config.MM_ROLE_ID] if stage == "mm_claim" and config.MM_ROLE_ID else False,
                everyone=False,
            ),
        )

    async def _notify_staff(self, guild: Optional[discord.Guild], text: str) -> None:
        if guild is None or not config.STAFF_LOG_CHANNEL_ID:
            return
        channel = guild.get_channel(config.STAFF_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.STAFF_LOG_CHANNEL_ID)
            except discord.HTTPException:
                log.warning("STAFF_LOG_CHANNEL_ID unavailable")
                return
        from utils.checks import safe_send

        await safe_send(channel, content=text, allowed_mentions=discord.AllowedMentions.none())

    async def _archive_ticket(self, ticket: Ticket, stamp: int) -> None:
        db = self.bot.db
        mode = config.ARCHIVE_MODE
        guild = self.bot.get_guild(ticket.guild_id)
        channel = guild.get_channel(ticket.channel_id) if guild and ticket.channel_id else None

        if channel is not None and isinstance(channel, discord.TextChannel):
            try:
                if mode == "delete":
                    await channel.delete(reason=f"{ticket.label}: auto-archive")
                elif mode == "lock":
                    new_name = channel.name
                    if not new_name.startswith("closed-"):
                        new_name = f"closed-{new_name}"[:100]
                    overwrites = dict(channel.overwrites)
                    for uid in (ticket.creator_id, ticket.partner_id):
                        if uid is None:
                            continue
                        member = guild.get_member(uid) if guild else None
                        if member is not None:
                            overwrites[member] = discord.PermissionOverwrite(
                                view_channel=True, send_messages=False, read_message_history=True
                            )
                    await channel.edit(
                        name=new_name, overwrites=overwrites, reason=f"{ticket.label}: auto-archive"
                    )
            except discord.HTTPException:
                log.exception("Archive action failed for %s", ticket.label)

        await db.mark_archived(ticket.id, stamp, mode)
        log.info("Archived %s (mode=%s)", ticket.label, mode)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MMCog(bot))
