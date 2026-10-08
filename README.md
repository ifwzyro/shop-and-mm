# MM Escrow — Discord Middleman Bot

A production-grade Middleman / MM Escrow system for Discord, built with **discord.py 2.6+**,
**SQLite (aiosqlite)** and persistent components.

All bot messages use Discord's **Components V2** UI — borderless (accent-less)
containers of clean text blocks, separators and controls, no legacy embeds.

Core guarantees:

- **Race-safe MM claiming** — atomic guarded SQL, exactly one MM can ever win a claim.
- **Restart-proof** — every ticket state lives in the database; views are re-registered
  and panels rebuilt after a restart.
- **Private reports** — full evidence goes only to a restricted report channel.
- **Timeout protection** — abandoned tickets are warned, flagged for review, and never
  auto-deleted.
- **No hardcoded IDs or emojis** — everything is driven by `config.py` and `emoji.json`.

---

## 1. Installation

```bash
git clone <your-repo-url> mm-escrow
cd mm-escrow

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env             # then put your bot token in .env
python main.py
```

Requires **Python 3.10+**.

## 2. Dependencies

| Package | Purpose |
|---|---|
| `discord.py >= 2.6.0` | Discord gateway, interactions, UI components |
| `aiosqlite >= 0.20.0` | Async SQLite (single connection, serialized transactions) |
| `python-dotenv >= 1.0.0` | Loads `.env` (bot token) |

## 3. Discord Developer Portal

At <https://discord.com/developers/applications> → your app → **Bot**:

1. Enable **Presence Intent**, **Server Members Intent** and **Message Content Intent**
   (Server Members is required for member/role validation and username resolution).
2. Copy the bot token into `.env` (`BOT_TOKEN`).
3. Use the OAuth2 URL generator with scopes `bot applications.commands`, permissions
   `Administrator` **or** the granular set in section 4, and invite the bot.

## 4. Required bot permissions

In the ticket category (and ticket channels) the bot needs:

- View Channels
- Send Messages / Embed Links / Attach Files
- Read Message History
- Manage Channels (create ticket channels, rename/lock on archive)
- Manage Permissions (grant traders access to their ticket)
- Manage Messages (optional, cleanup)
- Mention Everyone is **not** needed — the bot only pings the configured MM role.

In the report channel: View Channels, Send Messages, Embed Links.
The report channel must be restricted to staff only — Discord-side permissions are the
actual security boundary for reports.

## 5. Configuring `config.py`

`config.py` is the single source of truth for all IDs, timings and flags.

| Setting | Meaning |
|---|---|
| `GUILD_ID` | Primary guild (0 = sync globally, validate first joined guild) |
| `MM_CATEGORY_ID` | Category where ticket channels are created (0 = guild root) |
| `MM_ROLE_ID` | Role allowed to claim tickets (pinged when a trade needs an MM) |
| `MM_ADMIN_ROLE_ID` | Senior MM role (optional) |
| `STAFF_ROLE_ID` | Staff role (overrides, reports, recovery) |
| `REPORT_CHANNEL_ID` | **Required** — restricted channel that receives all reports |
| `STAFF_LOG_CHANNEL_ID` | Optional — timeout/flag/failure notifications |
| `TRANSCRIPT_CHANNEL_ID` | Optional — where transcripts are delivered (0 = file only) |
| `REQUIRED_ROLE_ID` | Optional — role required to open tickets (0 = everyone) |
| `TICKET_PREFIX` / `TICKET_NUMBER_PADDING` | Channel names: `mm-0001` (ID is always the DB row, never the name) |
| `MAX_ACTIVE_TICKETS_PER_USER` | Default `1` |
| `TICKET_COOLDOWN` | Seconds between creations per user (default `300`) |
| `ALLOW_SAME_ROLE` | `False` = exactly one Buyer + one Seller |
| `ALLOW_ROLE_CHANGE` | Traders may change roles before confirming |
| `DECLINE_REASON_MODAL` | Ask "Why are you declining?" |
| `CONFIRMATION_TIMEOUT` | Pending confirmations reset after this (default `1800`) |
| `CANCEL_REQUIRES_APPROVAL` | Trader cancels need MM/staff approval |
| `PARTNER_TIMEOUT`, `ROLE_TIMEOUT`, `MM_CLAIM_TIMEOUT`, `INACTIVE_TICKET_TIMEOUT` | Monitor thresholds |
| `AUTO_ARCHIVE_DELAY`, `ARCHIVE_MODE` | `lock` / `delete` / `none` after completion |
| `ALLOW_MULTIPLE_REPORTS`, `REPORT_CATEGORIES`, `DISPUTE_CATEGORIES` | Report behaviour |
| `TRANSCRIPT_ENABLED`, `TRANSCRIPT_INCLUDE_MESSAGES` | Transcript generation (message dumps are **off** by default) |
| `DATABASE_PATH`, `DEBUG`, `SYNC_COMMANDS` | Runtime |

Values of `0` mean "not configured"; startup validation logs a warning for missing
roles/channels and continues (nothing crashes unless the token itself is missing).

## 6. Configuring `emoji.json`

Every custom emoji used anywhere in the bot lives here:

```json
{
    "mm": "<:mm:123456789012345678>",
    "ticket": "<:ticket:123456789012345678>",
    "success": "<a:success:123456789012345678>"
}
```

- Use your own emoji strings (`<:name:id>` or animated `<a:name:id>`).
- Entries default to `""`. **Missing, empty or invalid entries simply render no emoji** —
  the bot never falls back to Unicode emoji.
- Keys: `mm, ticket, success, error, warning, money, seller, buyer, confirm, decline,
  report, security, user, clock, profile, contact, release, complete, cancel, staff,
  lock, refresh`.
- Read via `get_emoji(name)` / `button_emoji(name)` (`utils/emojis.py`).

## 7. Configuring `.env`

```bash
cp .env.example .env
```

```dotenv
BOT_TOKEN=your_bot_token_here
# optional:
# DATABASE_PATH=data/mm.db
```

`BOT_TOKEN` is read **only** from the environment — never from source. `.env` is
git-ignored.

## 8. Database initialization

Fully automatic. On startup the bot:

1. Creates `data/mm.db` (WAL mode, foreign keys on) if missing.
2. Creates all tables (`mm_tickets`, `mm_traders`, `mm_claims`, `mm_reports`,
   `mm_events`, `schema_migrations`) with indexes.
3. Applies versioned migrations — before touching an existing database it copies it to
   `data/backups/mm-YYYYMMDD-HHMMSS.db`.
4. **Never deletes or wipes existing data.**

Manual backup: copy `data/mm.db` while the bot is stopped.

## 9. Running the bot

```bash
python main.py
```

Startup log shows: database ready, loaded extensions, registered persistent views,
configuration warnings, synced command count. Normal button clicks are not logged —
only meaningful events (creation, claims, reports, errors).

Tests (no Discord connection needed):

```bash
python -m tests.test_database    # 24 logic/race tests
python -m tests.smoke_bot        # wiring: commands, persistent views, builders
```

## 10. `/mm setup`

`/mm setup` (staff-only) posts the escrow panel in the current channel:

> **## MM Escrow**
> Middleman tickets
> ———
> ● Open a ticket below.
> ● Ping your partner or paste their `Discord ID`.
> ● Only both traders and staff can view the ticket.
> **[Create MM Ticket]**
> ———
> ⚠ Must read: [MM ToS] · [Guidelines] *(text links, shown when the URLs are configured)*

The panel is a single borderless Components V2 container (no accent bar). Drop it
in a public channel; ticket channels are created hidden under `MM_CATEGORY_ID`
with only the creator, bot, MM role and staff able to see them.

## 11. The MM workflow

1. **Create** — `Create MM Ticket`. Duplicate active tickets are blocked (with a link
   to the existing one), cooldowns enforced, creation reserved atomically in the DB.
   Ticket code is `mm-0001` style; identity is always the DB `id`.
2. **Add partner** — creator opens the modal and enters an ID/mention (username
   resolution as fallback). Validated: exists in guild, not self, not a bot, not already
   in this ticket, no other active ticket (configurable). Partner gets channel access.
3. **Roles** — both traders pick *Seller* or *Buyer* from the select menu. Buyer+Buyer
   and Seller+Seller are rejected with a clear message (unless `ALLOW_SAME_ROLE`).
   Selections are stored per user in `mm_traders` and displayed in the panel.
4. **Confirmation** — a panel shows Buyer/Seller plus live `Confirmed / Waiting` state;
   both traders must press Confirm. Decline opens an optional reason modal and resets
   the confirmation (with restart button). Expiry (`CONFIRMATION_TIMEOUT`) resets
   automatically without notifying MMs.
5. **MM request** — when both confirm, the configured MM role is pinged and the
   **Claim MM** button appears.
6. **Claim** — only members with the MM role (not the traders) can claim; the claim is a
   single guarded `UPDATE ... WHERE status='WAITING_FOR_MM' AND claimed_mm_id IS NULL`
   inside a serialized transaction, so two simultaneous clicks yield exactly one winner —
   the loser sees *“This ticket has already been claimed by @MM.”*
7. **Assigned panel** — shows MM mention, ID, avatar and claim time with buttons:
   **MM Profile**, **Report MM**, **Contact MM**, **Release MM**, **Complete Trade**,
   **Cancel Ticket** — each permission-checked against live roles/state, never against
   the button's visible state.
8. **Completion** — requires confirmation, marks the ticket `COMPLETED`, disables
   controls, keeps the audit history, generates a transcript and archives after
   `AUTO_ARCHIVE_DELAY` (`lock` or `delete`).

All state transitions are recorded as `mm_events` rows (audit trail with actor,
metadata JSON and timestamp).

## 12. How reports work

1. Only the two traders can press **Report MM**; the MM can't report themselves;
   duplicate reports are blocked unless `ALLOW_MULTIPLE_REPORTS`.
2. The ticket shows a short *“Please check your DMs to submit the report.”* notice.
   If DMs are closed, the ticket tells the user how to fix it — nothing breaks.
3. In DMs: pick a category (`REPORT_CATEGORIES` select), then a modal collects the
   description. Input is re-validated against the database (trader, assigned MM,
   duplicates) — the select menu itself is never trusted.
4. The full report card (reporter, reported MM + avatar, ticket, roles, category,
   description, claim/report times, evidence: confirmations, declines, claim history,
   participants, optional jump links) is sent **only** to `REPORT_CHANNEL_ID` with
   Acknowledge / Resolve / Escalate buttons (staff-only). Failure to deliver is logged,
   the report is kept as `DELIVERY_FAILED`, and the reporter gets a safe message —
   the bot never crashes over it.
5. The public ticket only ever shows *“Report submitted. Our moderation team has
   received your report.”*
6. Scam-like categories (`DISPUTE_CATEGORIES`) move the ticket to `DISPUTED`; resolving
   the report restores the previous state, and the reporter is DM'd when
   `NOTIFY_REPORTER_ON_RESOLVE` is on.

`/mm reportinfo <id>` shows any report (staff).

## 13. Staff recovery

| Situation | Tool |
|---|---|
| Overview of active tickets, flagged tickets, open reports | `/mm staff` |
| Ticket details + audit timeline (also when the channel is gone) | `/mm ticketinfo ticket:MM-0042` |
| MM never claimed / stuck waiting | `/mm forceclaim ticket:MM-0042` (works unclaimed; claims are still atomic) |
| MM gone, lost the role, or inactive | `/mm release` → MM role re-pinged → claim again (or `/mm forceclaim`) |
| Channel deleted | Monitor flags it once and notifies `STAFF_LOG_CHANNEL_ID`; DB stays intact — `/mm close` still works |
| Abandoned ticket | Monitor warns the responsible parties once per stage, flags for review, never deletes |
| Hard close | `/mm close ticket:MM-0042 reason:...` → status `CLOSED`, panels disabled, transcript, archive |
| Report triage | Report channel buttons, or `/mm reportinfo` / resolve |

Every staff action writes an audit event (`MM_RELEASED`, `MM_CLAIMED` with
`via: force`, `TICKET_CLOSED`, `REPORT_STATUS_CHANGED`, …).

---

## Project layout

```
main.py                 entry point, command-tree error handling, wiring
config.py               all IDs, timings, flags, colors
emoji.json              all custom emojis (empty = no emoji, never Unicode)
requirements.txt
.env.example
database/               models, versioned migrations + backups, atomic queries
cogs/                   mm (commands + monitor), tickets, reports, staff
views/                  persistent panels & buttons (ticket, confirmation, MM, report, staff)
utils/                  emojis, permissions, checks, formatting, time, logging
tests/                  database logic/race tests + offline wiring smoke test
data/                   mm.db + backups/ (auto-created)
logs/                   bot.log + transcripts/ (auto-created)
```
