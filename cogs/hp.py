from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands


log = logging.getLogger(__name__)


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
CONTENT_DIR = BASE_DIR / "content"

HONEYPOT_FILE = DATA_DIR / "honeypot.json"
EMOJI_FILE = CONTENT_DIR / "emoji.json"

# Discord ban message deletion window:
# 5 hours = 18,000 seconds
DELETE_MESSAGE_SECONDS = 5 * 60 * 60


# ============================================================
# JSON HELPERS
# ============================================================

def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default

        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    except Exception:
        log.exception("Failed to load JSON file: %s", path)
        return default


def save_json(path: Path, data: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = path.with_suffix(".tmp")

        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                indent=4,
                ensure_ascii=False
            )

        temp_path.replace(path)

        return True

    except Exception:
        log.exception("Failed to save JSON file: %s", path)
        return False


def resolve_emoji(
    emoji_data: Any,
    key: str,
    fallback: str
) -> str:
    """
    Supports:

        "alert": "<:alert:123>"

    and:

        "alert": {
            "emoji": "<:alert:123>"
        }

    Also supports value/format/text fields.
    """

    if not isinstance(emoji_data, dict):
        return fallback

    value = emoji_data.get(key)

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        for field in (
            "emoji",
            "value",
            "format",
            "text"
        ):
            candidate = value.get(field)

            if isinstance(candidate, str):
                return candidate

    return fallback


# ============================================================
# HONEYPOT COG
# ============================================================

class Honeypot(commands.Cog):
    """
    Honeypot / spam-trap system.

    When someone sends a message inside the configured
    honeypot channel:

        1. Triggering message is deleted.
        2. Their messages from the last 5 hours across the
           entire server are deleted by Discord's ban action.
        3. User is banned.
        4. User is immediately unbanned.
        5. Counter is increased.

    This creates a Discord soft-ban.
    """

    def __init__(
        self,
        bot: commands.Bot
    ):
        self.bot = bot

        ensure_directories()

        # ----------------------------------------------------
        # Load honeypot data
        # ----------------------------------------------------

        self.data: dict[str, Any] = load_json(
            HONEYPOT_FILE,
            {
                "guilds": {}
            }
        )

        if not isinstance(self.data, dict):
            self.data = {
                "guilds": {}
            }

        if not isinstance(
            self.data.get("guilds"),
            dict
        ):
            self.data["guilds"] = {}

        # ----------------------------------------------------
        # Load emojis
        # ----------------------------------------------------

        emoji_data = load_json(
            EMOJI_FILE,
            {}
        )

        self.alert_emoji = resolve_emoji(
            emoji_data,
            "alert",
            "🚨"
        )

        self.hammer_emoji = resolve_emoji(
            emoji_data,
            "hammer",
            "🔨"
        )

        log.info(
            "Honeypot loaded | %s configured guild(s)",
            len(self.data["guilds"])
        )

    # ========================================================
    # DATA
    # ========================================================

    def get_guild_data(
        self,
        guild_id: int
    ) -> dict[str, Any]:

        guilds = self.data.setdefault(
            "guilds",
            {}
        )

        guild_data = guilds.setdefault(
            str(guild_id),
            {
                "channel_id": None,
                "soft_bans": 0
            }
        )

        if not isinstance(guild_data, dict):
            guild_data = {
                "channel_id": None,
                "soft_bans": 0
            }

            guilds[str(guild_id)] = guild_data

        return guild_data

    def get_honeypot_channel_id(
        self,
        guild_id: int
    ) -> int | None:

        guild_data = self.data.get(
            "guilds",
            {}
        ).get(str(guild_id))

        if not isinstance(guild_data, dict):
            return None

        channel_id = guild_data.get(
            "channel_id"
        )

        if channel_id is None:
            return None

        try:
            return int(channel_id)

        except (
            TypeError,
            ValueError
        ):
            return None

    def get_soft_ban_count(
        self,
        guild_id: int
    ) -> int:

        guild_data = self.get_guild_data(
            guild_id
        )

        try:
            return int(
                guild_data.get(
                    "soft_bans",
                    0
                )
            )

        except (
            TypeError,
            ValueError
        ):
            return 0

    def set_honeypot_channel(
        self,
        guild_id: int,
        channel_id: int
    ) -> None:

        guild_data = self.get_guild_data(
            guild_id
        )

        guild_data["channel_id"] = channel_id

        save_json(
            HONEYPOT_FILE,
            self.data
        )

    def increment_soft_bans(
        self,
        guild_id: int
    ) -> int:

        guild_data = self.get_guild_data(
            guild_id
        )

        current = self.get_soft_ban_count(
            guild_id
        )

        new_total = current + 1

        guild_data["soft_bans"] = new_total

        save_json(
            HONEYPOT_FILE,
            self.data
        )

        return new_total

    # ========================================================
    # COMPONENTS V2
    # ========================================================

    def build_honeypot_view(
        self,
        soft_bans: int
    ) -> discord.ui.LayoutView:

        view = discord.ui.LayoutView(
            timeout=None
        )

        container = discord.ui.Container(
            accent_color=discord.Color.red()
        )

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    f"# {self.alert_emoji} "
                    f"DO NOT POST IN THIS CHANNEL "
                    f"{self.alert_emoji}"
                )
            )
        )

        container.add_item(
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small
            )
        )

        # ----------------------------------------------------
        # DESCRIPTION
        # ----------------------------------------------------

        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    "## This is a monitored spam-trap channel.\n"
                    "Any message sent here triggers an "
                    "automatic soft ban: your recent messages "
                    "are deleted, then you are unbanned."
                )
            )
        )

        container.add_item(
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small
            )
        )

        # ----------------------------------------------------
        # COUNTER
        # ----------------------------------------------------

        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    f"**{self.hammer_emoji} "
                    f"Soft bans triggered:** {soft_bans}"
                )
            )
        )

        view.add_item(container)

        return view

    # ========================================================
    # HONEYPOT COMMAND
    # ========================================================

    @commands.command(
        name="honeypot",
        aliases=("honey",)
    )
    @commands.guild_only()
    @commands.has_permissions(
        manage_guild=True
    )
    @commands.bot_has_permissions(
        manage_messages=True,
        ban_members=True
    )
    async def honeypot(
        self,
        ctx: commands.Context
    ) -> None:

        guild = ctx.guild

        if guild is None:
            return

        channel = ctx.channel

        # ----------------------------------------------------
        # Only text channels
        # ----------------------------------------------------

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            await ctx.send(
                "This command can only be used in a text channel.",
                delete_after=8
            )
            return

        # ----------------------------------------------------
        # Save honeypot
        # ----------------------------------------------------

        self.set_honeypot_channel(
            guild.id,
            channel.id
        )

        total = self.get_soft_ban_count(
            guild.id
        )

        # ----------------------------------------------------
        # Remove command message
        # ----------------------------------------------------

        try:
            await ctx.message.delete()

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException
        ):
            pass

        # ----------------------------------------------------
        # Send Components V2 panel
        # ----------------------------------------------------

        try:
            await channel.send(
                view=self.build_honeypot_view(
                    total
                )
            )

        except discord.HTTPException:
            log.exception(
                "Failed to send honeypot panel | guild=%s channel=%s",
                guild.id,
                channel.id
            )

    # ========================================================
    # MESSAGE LISTENER
    # ========================================================

    @commands.Cog.listener()
    async def on_message(
        self,
        message: discord.Message
    ) -> None:

        # ----------------------------------------------------
        # DMs
        # ----------------------------------------------------

        if message.guild is None:
            return

        # ----------------------------------------------------
        # Bots
        # ----------------------------------------------------

        if message.author.bot:
            return

        # ----------------------------------------------------
        # Get configured honeypot
        # ----------------------------------------------------

        honeypot_channel_id = (
            self.get_honeypot_channel_id(
                message.guild.id
            )
        )

        if honeypot_channel_id is None:
            return

        # ----------------------------------------------------
        # Only trigger inside configured channel
        # ----------------------------------------------------

        if message.channel.id != honeypot_channel_id:
            return

        # ----------------------------------------------------
        # Ignore webhooks
        # ----------------------------------------------------

        if message.webhook_id is not None:
            return

        member = message.author

        if not isinstance(
            member,
            discord.Member
        ):
            return

        # ----------------------------------------------------
        # NEVER punish server owner
        # ----------------------------------------------------

        if member.id == message.guild.owner_id:
            try:
                await message.delete()

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                pass

            return

        # ----------------------------------------------------
        # NEVER punish administrators
        # ----------------------------------------------------

        if member.guild_permissions.administrator:
            try:
                await message.delete()

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                pass

            return

        # ----------------------------------------------------
        # Get bot member
        # ----------------------------------------------------

        bot_member = message.guild.me

        if bot_member is None:
            return

        # ----------------------------------------------------
        # Permission check
        # ----------------------------------------------------

        if not bot_member.guild_permissions.ban_members:
            log.warning(
                "Honeypot cannot operate in guild %s: "
                "missing Ban Members permission.",
                message.guild.id
            )
            return

        # ----------------------------------------------------
        # Role hierarchy
        # ----------------------------------------------------

        if member.top_role >= bot_member.top_role:
            log.warning(
                "Honeypot cannot punish %s (%s): "
                "role hierarchy prevents moderation.",
                member,
                member.id
            )

            # Still delete the triggering message.
            try:
                await message.delete()

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                pass

            return

        # ----------------------------------------------------
        # DELETE TRIGGERING MESSAGE
        # ----------------------------------------------------

        try:
            await message.delete()

        except discord.NotFound:
            pass

        except discord.Forbidden:
            log.warning(
                "Cannot delete triggering honeypot message "
                "in guild %s.",
                message.guild.id
            )

        except discord.HTTPException:
            pass

        # ----------------------------------------------------
        # SOFT BAN
        #
        # Discord deletes this user's messages from the
        # ENTIRE SERVER from the previous 5 HOURS.
        #
        # 5 hours = 18,000 seconds.
        # ----------------------------------------------------

        try:
            await member.ban(
                reason="Honeypot triggered",
                delete_message_seconds=DELETE_MESSAGE_SECONDS
            )

        except discord.NotFound:
            return

        except discord.Forbidden:
            log.warning(
                "Cannot ban %s (%s) in guild %s.",
                member,
                member.id,
                message.guild.id
            )
            return

        except discord.HTTPException:
            log.exception(
                "Discord API error while soft-banning "
                "%s (%s) in guild %s.",
                member,
                member.id,
                message.guild.id
            )
            return

        # ----------------------------------------------------
        # IMMEDIATELY UNBAN
        # ----------------------------------------------------

        try:
            await message.guild.unban(
                member,
                reason="Honeypot soft ban"
            )

        except discord.NotFound:
            # Already unbanned / ban no longer exists.
            pass

        except discord.Forbidden:
            log.warning(
                "Cannot unban %s (%s) in guild %s.",
                member,
                member.id,
                message.guild.id
            )
            return

        except discord.HTTPException:
            log.exception(
                "Failed to unban %s (%s) in guild %s.",
                member,
                member.id,
                message.guild.id
            )
            return

        # ----------------------------------------------------
        # INCREASE COUNTER
        # ----------------------------------------------------

        total = self.increment_soft_bans(
            message.guild.id
        )

        log.info(
            "HONEYPOT TRIGGERED | "
            "guild=%s | user=%s | deleted=5h | total=%s",
            message.guild.id,
            member.id,
            total
        )

        # ----------------------------------------------------
        # UPDATE PANEL
        # ----------------------------------------------------

        await self.update_honeypot_panel(
            message.channel,
            total
        )

    # ========================================================
    # UPDATE PANEL
    # ========================================================

    async def update_honeypot_panel(
        self,
        channel: discord.TextChannel,
        soft_bans: int
    ) -> None:

        """
        Finds the bot's honeypot panel among recent messages
        and updates its counter.

        Only scans the most recent 25 messages to keep this
        lightweight.
        """

        try:
            async for message in channel.history(
                limit=25
            ):

                if self.bot.user is None:
                    return

                if message.author.id != self.bot.user.id:
                    continue

                if not message.components:
                    continue

                try:
                    await message.edit(
                        view=self.build_honeypot_view(
                            soft_bans
                        )
                    )

                    return

                except discord.HTTPException:
                    continue

        except (
            discord.Forbidden,
            discord.HTTPException
        ):
            pass

    # ========================================================
    # COMMAND ERROR
    # ========================================================

    @honeypot.error
    async def honeypot_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError
    ) -> None:

        # ----------------------------------------------------
        # Missing user permission
        # ----------------------------------------------------

        if isinstance(
            error,
            commands.MissingPermissions
        ):
            await ctx.send(
                "You need **Manage Server** permission "
                "to configure the honeypot.",
                delete_after=8
            )
            return

        # ----------------------------------------------------
        # Missing bot permission
        # ----------------------------------------------------

        if isinstance(
            error,
            commands.BotMissingPermissions
        ):
            await ctx.send(
                "I need **Manage Messages** and "
                "**Ban Members** permissions.",
                delete_after=8
            )
            return

        # ----------------------------------------------------
        # Unexpected error
        # ----------------------------------------------------

        log.exception(
            "Unexpected honeypot command error",
            exc_info=error
        )


# ============================================================
# SETUP
# ============================================================

async def setup(
    bot: commands.Bot
) -> None:
    await bot.add_cog(
        Honeypot(bot)
    )