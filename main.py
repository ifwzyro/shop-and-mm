"""Entry point for the MM Escrow / Middleman bot.

Run with:  python main.py
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from utils.logging import setup_logging
from views import register_panel_view, register_report_views, register_ticket_views

log = logging.getLogger("mm")


class MMCommandTree(app_commands.CommandTree):
    """Global app-command error handling: friendly messages, logged tracebacks."""

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You can't use this command."
        elif isinstance(error, app_commands.CommandNotFound):
            return
        else:
            log.error(
                "Error in command %s",
                getattr(interaction.command, "qualified_name", "?"),
                exc_info=error,
            )
            message = "Something went wrong while running that command. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.exception("Could not send command error response")


class MiddlemanBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # required for member/role validation (enable in the portal)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=MMCommandTree,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=True, roles=True, replied_user=False
            ),
        )
        self.db: Database = None  # type: ignore[assignment]
        self.startup_event = asyncio.Event()

    async def setup_hook(self) -> None:
        # Database -------------------------------------------------------
        self.db = Database(config.DATABASE_PATH)
        await self.db.initialize()

        # Cogs (load order matters: cogs.mm defines the shared /mm group) --
        for extension in ("cogs.mm", "cogs.tickets", "cogs.reports", "cogs.staff", "cogs.hp"):
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        # Register the /mm group once every cog added its subcommands.
        from cogs.mm import mm_group

        self.tree.add_command(mm_group)

        # Persistent views ----------------------------------------------
        register_panel_view(self)
        active = await self.db.list_active_tickets()
        for ticket in active:
            register_ticket_views(self, ticket.id)
        register_report_views(self, await self.db.list_open_reports())
        log.info(
            "Persistent views registered (panel + %d ticket(s) + open reports)", len(active)
        )

    async def on_ready(self) -> None:
        if self.user:
            log.info("Logged in as %s (%s) | guilds=%d", self.user, self.user.id, len(self.guilds))

    async def close(self) -> None:
        await super().close()
        if self.db is not None:
            await self.db.close()
            log.info("Database connection closed")


async def main() -> None:
    setup_logging()
    log.info("Starting MM Escrow bot...")

    if not config.BOT_TOKEN:
        log.critical(
            "BOT_TOKEN is not set. Copy .env.example to .env and add your bot token, "
            "or export BOT_TOKEN in the environment."
        )
        raise SystemExit(1)

    async with MiddlemanBot() as bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")
