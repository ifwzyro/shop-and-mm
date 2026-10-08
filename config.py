"""
Central configuration for the MM Escrow / Middleman bot.

Every Discord ID, timing, permission, flag and UI setting lives here.
Cogs, views and database code must import values from this module —
never hard-code IDs, timeouts or emoji anywhere else.

Secrets are read from the environment (see .env.example). Nothing
secret is stored in this file.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Secrets (environment only — never hard-code)
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# ---------------------------------------------------------------------------
# Discord IDs  (0 = not configured; validated at startup, warnings logged)
# ---------------------------------------------------------------------------

GUILD_ID: int = 1539184406613327902                 # Primary guild (0 = use bot in any guild it is in)
MM_CATEGORY_ID: int = 1539184408362491914             # Category where ticket channels are created (0 = guild root)
MM_ROLE_ID: int = 1539192980613636096                  # Role allowed to claim MM tickets
MM_ADMIN_ROLE_ID: int = 1539188281302978660            # Optional: senior MM role (can claim + act as staff)
STAFF_ROLE_ID: int = 1541440609665949786            # Staff role (overrides, reports, recovery)
REPORT_CHANNEL_ID: int = 1557754877961048104           # Secret, heavily restricted report channel (required for reports)
STAFF_LOG_CHANNEL_ID: int = 1539228592016330812        # Optional: staff notifications (timeouts, flags, failures)
TRANSCRIPT_CHANNEL_ID: int =  1539228594289512539      # Optional: where transcripts are delivered (0 = file only)
REQUIRED_ROLE_ID: int = 0            # Optional: role required to create tickets (0 = everyone)

# ---------------------------------------------------------------------------
# Ticket settings
# ---------------------------------------------------------------------------

TICKET_PREFIX: str = "mm"                 # channel name: mm-0001, mm-0002 ...
TICKET_NUMBER_PADDING: int = 4
MAX_ACTIVE_TICKETS_PER_USER: int = 1
TICKET_COOLDOWN: int = 300                # seconds between ticket creations per user
ALLOW_PARTNER_WITH_ACTIVE_TICKET: bool = False  # may a user be added as partner while already in another ticket
TICKET_STAFF_SEES_TICKETS: bool = True    # staff role can view ticket channels
TICKET_MM_SEES_TICKETS: bool = True       # MM role can view ticket channels

# ---------------------------------------------------------------------------
# Trade flow
# ---------------------------------------------------------------------------

ALLOW_SAME_ROLE: bool = False             # True permits Buyer+Buyer / Seller+Seller
ALLOW_ROLE_CHANGE: bool = True            # traders may change role before confirmation starts
DECLINE_REASON_MODAL: bool = True         # ask "Why are you declining?" modal
CONFIRMATION_TIMEOUT: int = 1800          # seconds until pending confirmations reset
ALLOW_MM_ADMIN_CLAIM: bool = True         # MM admins may claim without the base MM role
AUTO_IN_PROGRESS: bool = False            # True: claim moves the ticket to IN_PROGRESS
CANCEL_REQUIRES_APPROVAL: bool = False    # if True, trader cancel needs MM/staff approval

# ---------------------------------------------------------------------------
# Timeouts (seconds) — monitored by the background task; tickets are never
# auto-deleted, only warned / flagged for review.
# ---------------------------------------------------------------------------

PARTNER_TIMEOUT: int = 3600
ROLE_TIMEOUT: int = 3600
MM_CLAIM_TIMEOUT: int = 1800
INACTIVE_TICKET_TIMEOUT: int = 86400
MONITOR_INTERVAL: int = 60                # background check interval

# ---------------------------------------------------------------------------
# Archive / close behaviour
# ---------------------------------------------------------------------------

AUTO_ARCHIVE_DELAY: int = 600             # delay after completion/cancel before archive (0 = never)
ARCHIVE_MODE: str = "lock"                # "lock" | "delete" | "none"

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

ALLOW_MULTIPLE_REPORTS: bool = False      # False = one active report per reporter per ticket
REPORT_CATEGORIES: list[str] = [
    "Scam / Fraud",
    "Refused to process trade",
    "Unprofessional behavior",
    "Delayed / inactive MM",
    "Incorrect handling",
    "Harassment",
    "Other",
]
DISPUTE_CATEGORIES: list[str] = ["Scam / Fraud"]   # reports that flag the ticket as DISPUTED
INCLUDE_JUMP_LINKS: bool = True           # include panel jump links in report evidence
NOTIFY_REPORTER_ON_RESOLVE: bool = True   # DM the reporter when staff resolves their report
PUBLIC_NOTICE_DELETE_AFTER: int = 30      # auto-delete for "check your DMs" notices (0 = keep)

# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

TRANSCRIPT_ENABLED: bool = True
TRANSCRIPT_INCLUDE_MESSAGES: bool = False # NEVER dumps message history unless explicitly enabled
TRANSCRIPT_MESSAGE_LIMIT: int = 200
TRANSCRIPT_SAVE_DIR: Path = BASE_DIR / "logs" / "transcripts"

# ---------------------------------------------------------------------------
# Database / logging
# ---------------------------------------------------------------------------

DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "mm.db")))
BACKUP_DIR: Path = BASE_DIR / "data" / "backups"

DEBUG: bool = False
LOG_FILE: Path = BASE_DIR / "logs" / "bot.log"
LOG_MAX_BYTES: int = 1_000_000
LOG_BACKUP_COUNT: int = 5

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

EMBED_COLOR: int = 0x5865F2       # blurple — primary panels
SUCCESS_COLOR: int = 0x57F287     # confirmations / completed
ERROR_COLOR: int = 0xED4245       # errors / reports / destructive
WARNING_COLOR: int = 0xFEE75C     # warnings / timeouts

MM_TOS_URL: str = ""              # link button on the setup panel (hidden when empty)
MM_GUIDELINES_URL: str = ""       # link button on the setup panel (hidden when empty)

SYNC_COMMANDS: bool = True        # sync slash commands on startup
