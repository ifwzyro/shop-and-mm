"""Trader confirmation flow: confirm / decline / restart + MM request dispatch."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import discord

import config
from database.models import Ticket, TicketStatus, Trader
from utils.checks import load_ticket_for_view, send_interaction, validate_role_pair
from utils.emojis import button_emoji
from utils.formatting import error_notice, success_notice, warning_notice
from utils.time import now
from views.base import (
    BaseModal,
    BaseView,
    PanelData,
    build_confirmation_data,
    build_mm_request_data,
    edit_panel,
    panel_container,
    panel_footer,
    refresh_status_panel,
    send_and_store,
)

log = logging.getLogger("mm.views.confirmation")


# ---------------------------------------------------------------------------
# message sync
# ---------------------------------------------------------------------------

async def sync_confirmation_message(
    bot,
    ticket: Ticket,
    traders: Sequence[Trader],
    *,
    state: str = "pending",
    detail: Optional[str] = None,
    stage: Optional[str] = None,
) -> bool:
    """Create or update the confirmation panel (Msg2) for the given state."""
    if stage is None:
        stage = {"pending": "confirm", "confirmed": "done"}.get(state, "reset")
    data = build_confirmation_data(ticket, traders, state=state, detail=detail)

    guild = bot.get_guild(ticket.guild_id)
    if guild is None:
        return False
    if await edit_panel(
        guild,
        ticket,
        "confirm_message_id",
        build=lambda: ConfirmationView(ticket.id, stage=stage, data=data),
    ):
        return True
    channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
    if channel is None:
        log.warning("No channel for confirmation panel of %s", ticket.label)
        return False
    message = await send_and_store(
        bot.db,
        channel,
        ticket,
        "confirm_message_id",
        view=ConfirmationView(ticket.id, stage=stage, data=data),
    )
    return message is not None


async def send_mm_request(bot, ticket: Ticket, force: bool = False) -> bool:
    """Post the 'MM Required' panel with the configured role ping (Msg3).

    Skips silently when a request message already exists unless *force* is set
    (used by restart recovery when the message was deleted).
    """
    if ticket.mm_message_id is not None and not force:
        return False
    guild = bot.get_guild(ticket.guild_id)
    if guild is None:
        return False
    channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
    if channel is None:
        log.warning("No channel for MM request of %s", ticket.label)
        return False

    content: Optional[str] = None
    allowed: Optional[discord.AllowedMentions] = None
    if config.MM_ROLE_ID:
        role = guild.get_role(config.MM_ROLE_ID)
        if role is not None:
            content = role.mention
            allowed = discord.AllowedMentions(roles=[role], users=False, everyone=False)
        else:
            log.warning("MM role %s missing — sending MM request without ping", config.MM_ROLE_ID)

    from views.mm_panel import ClaimView  # local import: avoid cycles

    fresh = await bot.db.get_ticket(ticket.id) or ticket
    if fresh.mm_message_id is not None and not force:
        return False
    message = await send_and_store(
        bot.db,
        channel,
        fresh,
        "mm_message_id",
        content=content,
        view=ClaimView(fresh.id, data=build_mm_request_data(fresh)),
        allowed_mentions=allowed,
    )
    if message is None:
        return False
    log.info("MM requested for %s", fresh.label)
    return True


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

async def handle_confirm(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction, ticket_id, statuses=(TicketStatus.WAITING_FOR_CONFIRMATION,)
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    trader = await db.get_trader(ticket.id, interaction.user.id)
    if trader is None:
        await send_interaction(
            interaction,
            view=error_notice("Not a trader", "Only the two traders in this ticket can confirm."),
            ephemeral=True,
        )
        return

    outcome = await db.confirm_trader(ticket.id, interaction.user.id, now(), config.ALLOW_SAME_ROLE)

    if outcome == "already":
        await send_interaction(
            interaction,
            view=success_notice(
                "Already confirmed",
                "You have already confirmed this trade — waiting for the other trader.",
            ),
            ephemeral=True,
        )
        return
    if outcome == "not_trader":
        await send_interaction(
            interaction,
            view=error_notice("Not a trader", "Only the two traders in this ticket can confirm."),
            ephemeral=True,
        )
        return
    if outcome == "bad_state":
        await send_interaction(
            interaction,
            view=error_notice(
                "Unavailable",
                "The confirmation is no longer active. Check the ticket panel for the current state.",
            ),
            ephemeral=True,
        )
        return
    if outcome == "roles_invalid":
        traders = await db.get_traders(ticket.id)
        _, conflict = validate_role_pair([t.trade_role for t in traders], config.ALLOW_SAME_ROLE)
        await send_interaction(
            interaction,
            view=error_notice("Invalid roles", conflict or "Role pair is invalid."),
            ephemeral=True,
        )
        return

    if outcome == "ok_partial":
        fresh = await db.get_ticket(ticket.id) or ticket
        traders = await db.get_traders(ticket.id)
        await edit_panel_via_interaction(
            interaction,
            ConfirmationView(
                ticket_id,
                stage="confirm",
                data=build_confirmation_data(fresh, traders, state="pending"),
            ),
        )
        return

    # Both traders confirmed.
    started = await db.begin_mm_request(ticket.id, now())
    fresh = await db.get_ticket(ticket.id) or ticket
    traders = await db.get_traders(ticket.id)
    await edit_panel_via_interaction(
        interaction,
        ConfirmationView(
            ticket_id,
            stage="done",
            data=build_confirmation_data(fresh, traders, state="confirmed"),
        ),
    )

    if started:
        await send_mm_request(interaction.client, fresh)
    else:
        # Monitor recovery will move CONFIRMED -> WAITING_FOR_MM if needed.
        log.error("begin_mm_request failed for %s after double confirmation", fresh.label)


async def edit_panel_via_interaction(
    interaction: discord.Interaction, view: discord.ui.LayoutView
) -> None:
    """Edit the clicked confirmation panel; fall back to a direct edit."""
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=view, embed=None)
        elif interaction.message is not None:
            await interaction.message.edit(view=view, embed=None)
        else:
            await send_interaction(interaction, view=view, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not update confirmation panel for interaction %s", interaction.id)


async def handle_decline(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction, ticket_id, statuses=(TicketStatus.WAITING_FOR_CONFIRMATION,)
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if await db.get_trader(ticket.id, interaction.user.id) is None:
        await send_interaction(
            interaction,
            view=error_notice("Not a trader", "Only the two traders in this ticket can decline."),
            ephemeral=True,
        )
        return
    if config.DECLINE_REASON_MODAL:
        await interaction.response.send_modal(DeclineModal(ticket_id))
    else:
        await apply_decline(interaction, ticket_id, None)


async def apply_decline(
    interaction: discord.Interaction, ticket_id: int, reason: Optional[str]
) -> None:
    """Reset confirmations and park both panels on the restart state."""
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(interaction, ticket_id)
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return

    ok = await db.reset_confirmation(
        ticket.id,
        now(),
        event_type="TRADER_DECLINED",
        actor_id=interaction.user.id,
        metadata={"reason": reason},
    )
    if not ok:
        await send_interaction(
            interaction,
            view=error_notice("Already reset", "The confirmation has already been reset."),
            ephemeral=True,
        )
        return

    fresh = await db.get_ticket(ticket.id) or ticket
    traders = await db.get_traders(fresh.id)

    await send_interaction(
        interaction,
        view=warning_notice(
            "Confirmation declined",
            "The confirmation was reset. Both traders can adjust roles and restart.",
        ),
        ephemeral=True,
    )
    detail = f"Declined by <@{interaction.user.id}>."
    if reason:
        detail = f"{detail}\nReason: {reason}"
    await sync_confirmation_message(interaction.client, fresh, traders, state="declined", detail=detail)
    await refresh_status_panel(interaction.client, fresh, traders)


async def handle_restart(interaction: discord.Interaction, ticket_id: int) -> None:
    db = interaction.client.db
    ticket, error = await load_ticket_for_view(
        interaction, ticket_id, statuses=(TicketStatus.ROLES_SELECTED,)
    )
    if error:
        await send_interaction(interaction, view=error_notice("Unavailable", error), ephemeral=True)
        return
    if await db.get_trader(ticket.id, interaction.user.id) is None:
        await send_interaction(
            interaction,
            view=error_notice("Not a trader", "Only the two traders in this ticket can do this."),
            ephemeral=True,
        )
        return

    traders = await db.get_traders(ticket.id)
    state, message = validate_role_pair([t.trade_role for t in traders], config.ALLOW_SAME_ROLE)
    if state != "valid":
        await send_interaction(
            interaction,
            view=error_notice(
                "Roles not ready",
                message or "Both traders must pick complementary roles first.",
            ),
            ephemeral=True,
        )
        return

    if not await db.start_confirmation(
        ticket.id, now(), now() + config.CONFIRMATION_TIMEOUT, config.ALLOW_SAME_ROLE
    ):
        await send_interaction(
            interaction,
            view=error_notice("Unavailable", "The ticket state changed — please try again."),
            ephemeral=True,
        )
        return

    fresh = await db.get_ticket(ticket.id) or ticket
    traders = await db.get_traders(fresh.id)
    await send_interaction(
        interaction,
        view=success_notice(
            "Confirmation restarted",
            "Both traders must confirm the trade again.",
        ),
        ephemeral=True,
    )
    await sync_confirmation_message(interaction.client, fresh, traders, state="pending")
    await refresh_status_panel(interaction.client, fresh, traders)


async def expire_confirmation(bot, ticket: Ticket) -> bool:
    """Monitor hook: reset an expired confirmation and update both panels."""
    db = bot.db
    ok = await db.reset_confirmation(
        ticket.id,
        now(),
        event_type="CONFIRMATION_EXPIRED",
        metadata={"expired_at": ticket.confirm_expires_at},
    )
    if not ok:
        return False
    fresh = await db.get_ticket(ticket.id) or ticket
    traders = await db.get_traders(fresh.id)
    await sync_confirmation_message(
        bot, fresh, traders, state="expired", detail="Confirmation timed out."
    )
    await refresh_status_panel(bot, fresh, traders)

    guild = bot.get_guild(fresh.guild_id)
    channel = guild.get_channel(fresh.channel_id) if guild and fresh.channel_id else None
    if channel is not None:
        from utils.checks import safe_send

        await safe_send(
            channel,
            content=f"The confirmation for **{fresh.label}** expired and was reset. "
            "Use **Restart Confirmation** when you're ready.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    return True


# ---------------------------------------------------------------------------
# modal
# ---------------------------------------------------------------------------

class DeclineModal(BaseModal):
    def __init__(self, ticket_id: int):
        super().__init__(title="Why are you declining?", ticket_id=ticket_id)
        self.reason_input = discord.ui.TextInput(
            label="Reason (optional)",
            custom_id=f"mm:decline_reason:{ticket_id}",
            placeholder="Let the other trader know why",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason_input.value).strip() or None
        await apply_decline(interaction, self.ticket_id, reason)


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

class ConfirmationView(BaseView):
    """Confirmation panel across its lifecycle.

    Stages: ``confirm`` (Confirm/Decline), ``reset`` (Restart),
    ``done`` (disabled), ``all`` (registry for restart recovery).
    """

    def __init__(self, ticket_id: int, stage: str = "all", *, data: Optional[PanelData] = None):
        super().__init__(ticket_id=ticket_id)
        self.stage = stage
        data = data or PanelData("MM Trade Confirmation")
        container = panel_container(data)

        if stage in ("confirm", "done", "all"):
            disabled = stage == "done"
            confirm = discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.success,
                custom_id=f"mm:confirm:{ticket_id}",
                emoji=button_emoji("confirm"),
                disabled=disabled,
            )
            confirm.callback = self._on_confirm
            container.add_item(confirm)
            decline = discord.ui.Button(
                label="Decline",
                style=discord.ButtonStyle.danger,
                custom_id=f"mm:decline:{ticket_id}",
                emoji=button_emoji("decline"),
                disabled=disabled,
            )
            decline.callback = self._on_decline
            container.add_item(decline)

        if stage in ("reset", "all"):
            restart = discord.ui.Button(
                label="Restart Confirmation",
                style=discord.ButtonStyle.primary,
                custom_id=f"mm:restart:{ticket_id}",
                emoji=button_emoji("refresh"),
            )
            restart.callback = self._on_restart
            container.add_item(restart)

        panel_footer(container, data)
        self.add_item(container)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await handle_confirm(interaction, self.ticket_id)

    async def _on_decline(self, interaction: discord.Interaction) -> None:
        await handle_decline(interaction, self.ticket_id)

    async def _on_restart(self, interaction: discord.Interaction) -> None:
        await handle_restart(interaction, self.ticket_id)
