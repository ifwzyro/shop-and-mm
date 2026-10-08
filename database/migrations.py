"""Schema migrations with versioning and pre-migration backups.

Rules:
- Tables are created automatically if missing.
- Migrations are additive and versioned; existing data is never wiped.
- Before applying any migration to an existing database, a timestamped
  copy is written to ``config.BACKUP_DIR``.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import aiosqlite

import config

log = logging.getLogger("mm.db.migrations")

SCHEMA_VERSION = 1

Migration = List[str]  # ordered list of SQL statements

_BASE_TABLES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS mm_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_number INTEGER NOT NULL UNIQUE,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER,
        creator_id INTEGER NOT NULL,
        partner_id INTEGER,
        status TEXT NOT NULL DEFAULT 'CREATED',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        closed_at INTEGER,
        claimed_mm_id INTEGER,
        claimed_at INTEGER,
        partner_added_at INTEGER,
        confirm_started_at INTEGER,
        confirm_expires_at INTEGER,
        mm_requested_at INTEGER,
        last_activity_at INTEGER NOT NULL DEFAULT 0,
        stage_started_at INTEGER NOT NULL DEFAULT 0,
        warned_stage TEXT,
        flagged INTEGER NOT NULL DEFAULT 0,
        channel_missing INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        cancel_reason TEXT,
        status_message_id INTEGER,
        confirm_message_id INTEGER,
        mm_message_id INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_channel ON mm_tickets (channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_guild ON mm_tickets (guild_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_creator ON mm_tickets (creator_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_partner ON mm_tickets (partner_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_claimed_mm ON mm_tickets (claimed_mm_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON mm_tickets (status)",
    """
    CREATE TABLE IF NOT EXISTS mm_traders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES mm_tickets (id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        trade_role TEXT,
        confirmed INTEGER NOT NULL DEFAULT 0,
        confirmed_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (ticket_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_traders_ticket ON mm_traders (ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_traders_user ON mm_traders (user_id)",
    """
    CREATE TABLE IF NOT EXISTS mm_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES mm_tickets (id) ON DELETE CASCADE,
        mm_id INTEGER NOT NULL,
        claimed_at INTEGER NOT NULL,
        released_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_claims_ticket ON mm_claims (ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_claims_mm ON mm_claims (mm_id)",
    """
    CREATE TABLE IF NOT EXISTS mm_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES mm_tickets (id) ON DELETE CASCADE,
        reporter_id INTEGER NOT NULL,
        reported_mm_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        channel_id INTEGER,
        message_id INTEGER,
        resolved_at INTEGER,
        resolved_by INTEGER,
        prev_ticket_status TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_ticket ON mm_reports (ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_reporter ON mm_reports (reporter_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_mm ON mm_reports (reported_mm_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_status ON mm_reports (status)",
    """
    CREATE TABLE IF NOT EXISTS mm_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES mm_tickets (id) ON DELETE CASCADE,
        actor_id INTEGER,
        event_type TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ticket ON mm_events (ticket_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON mm_events (event_type)",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL
    )
    """,
]

# version -> ordered SQL statements. Never edit an applied migration;
# add a new version instead.
MIGRATIONS: Dict[int, Migration] = {
    1: _BASE_TABLES,
}


async def _current_version(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    await conn.commit()
    async with conn.execute("SELECT MAX(version) AS v FROM schema_migrations") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _backup_existing_db() -> None:
    """Copy the current database file to BACKUP_DIR before migrating."""
    db_path = Path(config.DATABASE_PATH)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.BACKUP_DIR / f"{db_path.stem}-{stamp}{db_path.suffix}"
    # Copy sidecar WAL/SHM files too so the backup is consistent.
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(target) + suffix))
    log.info("Database backup created at %s", target)


async def migrate(conn: aiosqlite.Connection) -> int:
    """Apply all pending migrations. Returns the resulting schema version."""
    current = await _current_version(conn)
    pending = sorted(v for v in MIGRATIONS if v > current)
    if not pending:
        return current

    if current > 0:
        # Existing database with pending changes -> back up first.
        try:
            _backup_existing_db()
        except OSError:
            log.exception("Could not create database backup; aborting migration")
            raise

    for version in pending:
        statements = MIGRATIONS[version]
        log.info("Applying schema migration %d", version)
        try:
            for stmt in statements:
                await conn.execute(stmt)
            await conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )
            await conn.commit()
        except aiosqlite.Error:
            log.exception("Migration %d failed; database left at version %d", version, current)
            raise
        current = version

    return current
