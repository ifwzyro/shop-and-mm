"""Async SQLite access layer.

Design rules:
- One shared connection; every operation runs under a single asyncio lock so
  multi-statement transactions can never interleave (race-condition safety).
- All state transitions are *guarded atomic UPDATEs*: the SQL WHERE clause
  encodes the legal precondition and callers check ``rowcount``. Check-then-set
  patterns are never used for claims, confirmations, reports or closures.
- Nothing important lives only in memory; everything here survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional, Sequence, Union

import aiosqlite

import config
from database.migrations import migrate
from database.models import (
    Claim,
    Event,
    Report,
    ReportStatus,
    Ticket,
    TicketStatus,
    Trader,
)

log = logging.getLogger("mm.db")


class TicketExistsError(Exception):
    """Raised when the user already has an active MM ticket."""

    def __init__(self, ticket: Ticket):
        self.ticket = ticket
        super().__init__(f"active ticket exists: {ticket.label}")


class CooldownError(Exception):
    """Raised when a user tries to create tickets too quickly."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"cooldown active for {self.retry_after}s")


@dataclass
class ClaimResult:
    ok: bool
    claimed_by: Optional[int] = None
    reason: Optional[str] = None  # 'missing' | 'state'
    status: Optional[str] = None


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


class Database:
    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        async with self._lock:
            version = await migrate(self._conn)
        log.info("Database ready at %s (schema v%d)", self.path, version)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not initialized")
        return self._conn

    # ------------------------------------------------------------------
    # transaction / read helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialized IMMEDIATE transaction; rolls back on any exception."""
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        async with self._lock:
            async with await self.conn.execute(sql, params) as cur:
                return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            async with await self.conn.execute(sql, params) as cur:
                return await cur.fetchall()

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------

    async def create_ticket(self, guild_id: int, creator_id: int, now: Optional[int] = None) -> Ticket:
        """Atomically reserve a new ticket (duplicate + cooldown gates inside the tx)."""
        now = int(now if now is not None else time.time())
        active_sql = (
            "SELECT * FROM mm_tickets "
            f"WHERE (creator_id = ? OR partner_id = ?) AND status IN ({_placeholders(tuple(TicketStatus.ACTIVE))}) "
            "ORDER BY id DESC"
        )
        async with self._tx() as conn:
            async with await conn.execute(active_sql, (creator_id, creator_id, *tuple(TicketStatus.ACTIVE))) as cur:
                rows = await cur.fetchall()
            if rows:
                raise TicketExistsError(Ticket.from_row(rows[0]))

            async with await conn.execute(
                "SELECT MAX(created_at) AS last FROM mm_tickets WHERE creator_id = ?", (creator_id,)
            ) as cur:
                last = await cur.fetchone()
            if last and last["last"] is not None:
                elapsed = now - int(last["last"])
                if elapsed < config.TICKET_COOLDOWN:
                    raise CooldownError(config.TICKET_COOLDOWN - elapsed)

            async with await conn.execute(
                "SELECT COALESCE(MAX(ticket_number), 0) + 1 AS n FROM mm_tickets"
            ) as cur:
                number_row = await cur.fetchone()
            number = int(number_row["n"])

            cur = await conn.execute(
                """
                INSERT INTO mm_tickets (
                    ticket_number, guild_id, channel_id, creator_id, partner_id, status,
                    created_at, updated_at, last_activity_at, stage_started_at
                ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (number, guild_id, creator_id, TicketStatus.CREATED, now, now, now, now),
            )
            ticket_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, creator_id, "TICKET_CREATED", json.dumps({"number": number}), now),
            )
            async with await conn.execute("SELECT * FROM mm_tickets WHERE id = ?", (ticket_id,)) as cur2:
                row = await cur2.fetchone()
        return Ticket.from_row(row)

    async def set_channel(self, ticket_id: int, channel_id: int, now: int) -> bool:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET channel_id = ?, updated_at = ?, channel_missing = 0 WHERE id = ?",
                (channel_id, now, ticket_id),
            )
            return cur.rowcount == 1

    async def set_status(
        self,
        ticket_id: int,
        status: str,
        *,
        now: int,
        expect: Union[str, Iterable[str]],
        actor_id: Optional[int] = None,
        event_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Guarded status transition. Returns False when the precondition failed."""
        if status not in TicketStatus.ALL:
            raise ValueError(f"unknown ticket status {status!r}")
        expected = (expect,) if isinstance(expect, str) else tuple(expect)
        if not expected:
            raise ValueError("expect must not be empty")
        async with self._tx() as conn:
            cur = await conn.execute(
                f"UPDATE mm_tickets SET status = ?, stage_started_at = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({_placeholders(expected)})",
                (status, now, now, ticket_id, *expected),
            )
            if cur.rowcount != 1:
                return False
            if event_type:
                await conn.execute(
                    "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ticket_id, actor_id, event_type, json.dumps(metadata or {}), now),
                )
            return True

    async def set_message_id(self, ticket_id: int, field: str, message_id: int) -> bool:
        allowed = {"status_message_id", "confirm_message_id", "mm_message_id"}
        if field not in allowed:
            raise ValueError(f"invalid message field {field!r}")
        async with self._tx() as conn:
            cur = await conn.execute(
                f"UPDATE mm_tickets SET {field} = ? WHERE id = ?", (message_id, ticket_id)
            )
            return cur.rowcount == 1

    async def get_ticket(self, ticket_id: int) -> Optional[Ticket]:
        row = await self._fetchone("SELECT * FROM mm_tickets WHERE id = ?", (ticket_id,))
        return Ticket.from_row(row) if row else None

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Ticket]:
        row = await self._fetchone("SELECT * FROM mm_tickets WHERE channel_id = ?", (channel_id,))
        return Ticket.from_row(row) if row else None

    async def get_ticket_by_number(self, guild_id: int, number: int) -> Optional[Ticket]:
        row = await self._fetchone(
            "SELECT * FROM mm_tickets WHERE guild_id = ? AND ticket_number = ?", (guild_id, number)
        )
        return Ticket.from_row(row) if row else None

    async def find_active_ticket_for_user(self, user_id: int) -> Optional[Ticket]:
        sql = (
            "SELECT * FROM mm_tickets "
            f"WHERE (creator_id = ? OR partner_id = ?) AND status IN ({_placeholders(tuple(TicketStatus.ACTIVE))}) "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await self._fetchone(sql, (user_id, user_id, *tuple(TicketStatus.ACTIVE)))
        return Ticket.from_row(row) if row else None

    async def list_active_tickets(self) -> list[Ticket]:
        sql = (
            "SELECT * FROM mm_tickets "
            f"WHERE status IN ({_placeholders(tuple(TicketStatus.ACTIVE))}) ORDER BY id"
        )
        rows = await self._fetchall(sql, tuple(TicketStatus.ACTIVE))
        return [Ticket.from_row(r) for r in rows]

    async def list_archivable(self, deadline: int) -> list[Ticket]:
        """Terminal tickets whose archive delay has elapsed."""
        sql = (
            "SELECT * FROM mm_tickets "
            "WHERE archived = 0 AND closed_at IS NOT NULL AND closed_at <= ? AND status != ? "
            "ORDER BY id"
        )
        rows = await self._fetchall(sql, (deadline, TicketStatus.CLOSED))
        return [Ticket.from_row(r) for r in rows]

    async def touch(self, ticket_id: int, now: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE mm_tickets SET last_activity_at = ?, updated_at = ? WHERE id = ?",
                (now, now, ticket_id),
            )

    # ------------------------------------------------------------------
    # partner + traders
    # ------------------------------------------------------------------

    async def add_partner(self, ticket_id: int, partner_id: int, now: int) -> bool:
        """Atomically assign the trading partner and create both trader rows."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET partner_id = ?, status = ?, partner_added_at = ?, "
                "stage_started_at = ?, updated_at = ?, last_activity_at = ?, warned_stage = NULL "
                "WHERE id = ? AND partner_id IS NULL AND status IN (?, ?)",
                (
                    partner_id,
                    TicketStatus.PARTNER_ADDED,
                    now,
                    now,
                    now,
                    now,
                    ticket_id,
                    TicketStatus.CREATED,
                    TicketStatus.WAITING_FOR_PARTNER,
                ),
            )
            if cur.rowcount != 1:
                return False
            async with await conn.execute(
                "SELECT creator_id FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur2:
                trow = await cur2.fetchone()
            if trow is None:
                return False
            creator_id = trow["creator_id"]
            await conn.execute(
                "INSERT OR IGNORE INTO mm_traders (ticket_id, user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_id, creator_id, now, now),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO mm_traders (ticket_id, user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_id, partner_id, now, now),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, creator_id, "PARTNER_ADDED", json.dumps({"partner_id": partner_id}), now),
            )
            return True

    async def get_traders(self, ticket_id: int) -> list[Trader]:
        rows = await self._fetchall(
            "SELECT * FROM mm_traders WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        )
        return [Trader.from_row(r) for r in rows]

    async def get_trader(self, ticket_id: int, user_id: int) -> Optional[Trader]:
        row = await self._fetchone(
            "SELECT * FROM mm_traders WHERE ticket_id = ? AND user_id = ?", (ticket_id, user_id)
        )
        return Trader.from_row(row) if row else None

    async def set_trader_role(
        self, ticket_id: int, user_id: int, role: str, now: int
    ) -> Optional[Trader]:
        """Store a trader's role; any role change resets ALL confirmations.

        Returns the updated trader row, or None when the interaction was not
        allowed (unknown trader or ticket no longer awaiting roles).
        """
        if role not in ("BUYER", "SELLER"):
            raise ValueError(f"invalid trade role {role!r}")
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT status, partner_id FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur:
                trow = await cur.fetchone()
            if trow is None:
                return None
            if trow["status"] not in (
                TicketStatus.PARTNER_ADDED,
                TicketStatus.WAITING_FOR_ROLES,
                TicketStatus.ROLES_SELECTED,
                TicketStatus.WAITING_FOR_CONFIRMATION,
            ):
                return None

            async with await conn.execute(
                "SELECT trade_role FROM mm_traders WHERE ticket_id = ? AND user_id = ?",
                (ticket_id, user_id),
            ) as cur2:
                my_row = await cur2.fetchone()
            if my_row is None:
                return None
            previous = my_row["trade_role"]

            # A role change invalidates any pending confirmations.
            await conn.execute(
                "UPDATE mm_traders SET confirmed = 0, confirmed_at = NULL, updated_at = ? "
                "WHERE ticket_id = ?",
                (now, ticket_id),
            )
            await conn.execute(
                "UPDATE mm_traders SET trade_role = ?, updated_at = ? "
                "WHERE ticket_id = ? AND user_id = ?",
                (role, now, ticket_id, user_id),
            )
            await conn.execute(
                "UPDATE mm_tickets SET updated_at = ?, last_activity_at = ? WHERE id = ?",
                (now, now, ticket_id),
            )
            event_type = "ROLE_SELECTED" if previous is None else "ROLE_CHANGED"
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    user_id,
                    event_type,
                    json.dumps({"role": role, "previous": previous}),
                    now,
                ),
            )
            async with await conn.execute(
                "SELECT * FROM mm_traders WHERE ticket_id = ? AND user_id = ?",
                (ticket_id, user_id),
            ) as cur3:
                row = await cur3.fetchone()
        return Trader.from_row(row) if row else None

    # ------------------------------------------------------------------
    # confirmation flow
    # ------------------------------------------------------------------

    async def start_confirmation(self, ticket_id: int, now: int, expires_at: int, allow_same: bool) -> bool:
        """Move a valid role pair into WAITING_FOR_CONFIRMATION and reset flags."""
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT * FROM mm_traders WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            ) as cur:
                traders = await cur.fetchall()
            if len(traders) < 2:
                return False
            roles = [t["trade_role"] for t in traders]
            if any(r not in ("BUYER", "SELLER") for r in roles):
                return False
            if roles[0] == roles[1] and not allow_same:
                return False

            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, confirm_started_at = ?, confirm_expires_at = ?, "
                "stage_started_at = ?, updated_at = ?, warned_stage = NULL "
                "WHERE id = ? AND status IN (?, ?, ?, ?)",
                (
                    TicketStatus.WAITING_FOR_CONFIRMATION,
                    now,
                    expires_at,
                    now,
                    now,
                    ticket_id,
                    TicketStatus.ROLES_SELECTED,
                    TicketStatus.WAITING_FOR_ROLES,
                    TicketStatus.PARTNER_ADDED,
                    TicketStatus.WAITING_FOR_CONFIRMATION,
                ),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "UPDATE mm_traders SET confirmed = 0, confirmed_at = NULL WHERE ticket_id = ?",
                (ticket_id,),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (ticket_id, "CONFIRMATION_STARTED", json.dumps({"expires_at": expires_at}), now),
            )
            return True

    async def confirm_trader(
        self, ticket_id: int, user_id: int, now: int, allow_same: bool
    ) -> str:
        """Confirm one trader. Returns: ok_partial | ok_both | already | bad_state | not_trader | roles_invalid."""
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT status FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur:
                trow = await cur.fetchone()
            if trow is None or trow["status"] != TicketStatus.WAITING_FOR_CONFIRMATION:
                return "bad_state"

            async with await conn.execute(
                "SELECT * FROM mm_traders WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            ) as cur2:
                traders = await cur2.fetchall()
            mine = next((t for t in traders if t["user_id"] == user_id), None)
            if mine is None:
                return "not_trader"
            if mine["confirmed"]:
                return "already"
            roles = [t["trade_role"] for t in traders]
            if len(roles) < 2 or any(r not in ("BUYER", "SELLER") for r in roles):
                return "roles_invalid"
            if roles[0] == roles[1] and not allow_same:
                return "roles_invalid"

            await conn.execute(
                "UPDATE mm_traders SET confirmed = 1, confirmed_at = ?, updated_at = ? "
                "WHERE ticket_id = ? AND user_id = ?",
                (now, now, ticket_id, user_id),
            )
            await conn.execute(
                "UPDATE mm_tickets SET last_activity_at = ?, updated_at = ? WHERE id = ?",
                (now, now, ticket_id),
            )
            async with await conn.execute(
                "SELECT COUNT(*) AS n FROM mm_traders "
                "WHERE ticket_id = ? AND confirmed = 1 AND trade_role IS NOT NULL",
                (ticket_id,),
            ) as cur3:
                count_row = await cur3.fetchone()
            both = int(count_row["n"]) >= 2
            if both:
                cur = await conn.execute(
                    "UPDATE mm_tickets SET status = ?, stage_started_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (TicketStatus.CONFIRMED, now, now, ticket_id, TicketStatus.WAITING_FOR_CONFIRMATION),
                )
                if cur.rowcount != 1:
                    return "bad_state"
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    user_id,
                    "TRADER_CONFIRMED",
                    json.dumps({"role": mine["trade_role"], "both": both}),
                    now,
                ),
            )
            return "ok_both" if both else "ok_partial"

    async def begin_mm_request(self, ticket_id: int, now: int) -> bool:
        """CONFIRMED -> WAITING_FOR_MM (guarded)."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, mm_requested_at = ?, stage_started_at = ?, "
                "updated_at = ?, warned_stage = NULL WHERE id = ? AND status = ?",
                (
                    TicketStatus.WAITING_FOR_MM,
                    now,
                    now,
                    now,
                    ticket_id,
                    TicketStatus.CONFIRMED,
                ),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, NULL, ?, '{}', ?)",
                (ticket_id, "MM_REQUESTED", now),
            )
            return True

    async def reset_confirmation(
        self,
        ticket_id: int,
        now: int,
        *,
        event_type: str,
        actor_id: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Reset pending confirmations back to ROLES_SELECTED (guarded)."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, confirm_started_at = NULL, confirm_expires_at = NULL, "
                "stage_started_at = ?, updated_at = ?, last_activity_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    TicketStatus.ROLES_SELECTED,
                    now,
                    now,
                    now,
                    ticket_id,
                    TicketStatus.WAITING_FOR_CONFIRMATION,
                ),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "UPDATE mm_traders SET confirmed = 0, confirmed_at = NULL WHERE ticket_id = ?",
                (ticket_id,),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, actor_id, event_type, json.dumps(metadata or {}), now),
            )
            return True

    # ------------------------------------------------------------------
    # MM claims (atomic — the single most important section)
    # ------------------------------------------------------------------

    async def claim_ticket(self, ticket_id: int, mm_id: int, now: int) -> ClaimResult:
        """Atomically claim a WAITING_FOR_MM ticket. Exactly one winner, ever."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET claimed_mm_id = ?, claimed_at = ?, status = ?, "
                "stage_started_at = ?, updated_at = ?, last_activity_at = ?, warned_stage = NULL "
                "WHERE id = ? AND status = ? AND claimed_mm_id IS NULL",
                (mm_id, now, TicketStatus.MM_CLAIMED, now, now, now, ticket_id, TicketStatus.WAITING_FOR_MM),
            )
            if cur.rowcount == 1:
                await conn.execute(
                    "INSERT INTO mm_claims (ticket_id, mm_id, claimed_at) VALUES (?, ?, ?)",
                    (ticket_id, mm_id, now),
                )
                await conn.execute(
                    "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ticket_id, mm_id, "MM_CLAIMED", json.dumps({"via": "claim"}), now),
                )
                return ClaimResult(ok=True)

            async with await conn.execute(
                "SELECT claimed_mm_id, status FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur2:
                row = await cur2.fetchone()
            if row is None:
                return ClaimResult(ok=False, reason="missing")
            if row["claimed_mm_id"] is not None:
                return ClaimResult(ok=False, claimed_by=row["claimed_mm_id"])
            return ClaimResult(ok=False, reason="state", status=row["status"])

    async def force_claim_ticket(
        self, ticket_id: int, mm_id: int, actor_id: int, now: int
    ) -> ClaimResult:
        """Staff claim of an *unclaimed* active ticket (still fully guarded)."""
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT claimed_mm_id, status FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return ClaimResult(ok=False, reason="missing")
            if row["claimed_mm_id"] is not None:
                return ClaimResult(ok=False, claimed_by=row["claimed_mm_id"])
            if row["status"] not in TicketStatus.ACTIVE:
                return ClaimResult(ok=False, reason="state", status=row["status"])

            cur = await conn.execute(
                "UPDATE mm_tickets SET claimed_mm_id = ?, claimed_at = ?, status = ?, "
                "stage_started_at = ?, updated_at = ?, last_activity_at = ?, warned_stage = NULL "
                "WHERE id = ? AND claimed_mm_id IS NULL",
                (mm_id, now, TicketStatus.MM_CLAIMED, now, now, now, ticket_id),
            )
            if cur.rowcount != 1:
                async with await conn.execute(
                    "SELECT claimed_mm_id FROM mm_tickets WHERE id = ?", (ticket_id,)
                ) as cur2:
                    again = await cur2.fetchone()
                if again and again["claimed_mm_id"] is not None:
                    return ClaimResult(ok=False, claimed_by=again["claimed_mm_id"])
                return ClaimResult(ok=False, reason="state")

            await conn.execute(
                "INSERT INTO mm_claims (ticket_id, mm_id, claimed_at) VALUES (?, ?, ?)",
                (ticket_id, mm_id, now),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    actor_id,
                    "MM_CLAIMED",
                    json.dumps({"via": "force", "mm_id": mm_id}),
                    now,
                ),
            )
            return ClaimResult(ok=True)

    async def release_ticket(
        self, ticket_id: int, actor_id: int, now: int, reason: Optional[str] = None
    ) -> bool:
        """Release the assigned MM and return the ticket to WAITING_FOR_MM."""
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT claimed_mm_id FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None or row["claimed_mm_id"] is None:
                return False
            released_mm = row["claimed_mm_id"]
            cur = await conn.execute(
                "UPDATE mm_tickets SET claimed_mm_id = NULL, claimed_at = NULL, status = ?, "
                "stage_started_at = ?, updated_at = ?, last_activity_at = ?, warned_stage = NULL "
                "WHERE id = ? AND claimed_mm_id IS NOT NULL AND status IN "
                f"({_placeholders(tuple(TicketStatus.CLAIMED))})",
                (TicketStatus.WAITING_FOR_MM, now, now, now, ticket_id, *tuple(TicketStatus.CLAIMED)),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "UPDATE mm_claims SET released_at = ? WHERE ticket_id = ? AND released_at IS NULL",
                (now, ticket_id),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    actor_id,
                    "MM_RELEASED",
                    json.dumps({"released_mm_id": released_mm, "reason": reason}),
                    now,
                ),
            )
            return True

    async def complete_ticket(self, ticket_id: int, actor_id: int, now: int) -> bool:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, closed_at = ?, updated_at = ? "
                "WHERE id = ? AND claimed_mm_id IS NOT NULL AND status IN "
                f"({_placeholders(tuple(TicketStatus.CLAIMED))})",
                (TicketStatus.COMPLETED, now, now, ticket_id, *tuple(TicketStatus.CLAIMED)),
            )
            if cur.rowcount != 1:
                return False
            async with await conn.execute(
                "SELECT claimed_mm_id FROM mm_tickets WHERE id = ?", (ticket_id,)
            ) as cur2:
                row = await cur2.fetchone()
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    actor_id,
                    "TICKET_COMPLETED",
                    json.dumps({"mm_id": row["claimed_mm_id"] if row else None}),
                    now,
                ),
            )
            return True

    async def cancel_ticket(
        self, ticket_id: int, actor_id: int, now: int, reason: Optional[str] = None
    ) -> bool:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, closed_at = ?, cancel_reason = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({_placeholders(tuple(TicketStatus.ACTIVE))})",
                (TicketStatus.CANCELLED, now, reason, now, ticket_id, *tuple(TicketStatus.ACTIVE)),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, actor_id, "TICKET_CANCELLED", json.dumps({"reason": reason}), now),
            )
            return True

    async def close_ticket(
        self, ticket_id: int, actor_id: int, now: int, reason: Optional[str] = None
    ) -> bool:
        """Staff force-close from any non-closed state."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET status = ?, closed_at = ?, updated_at = ? "
                "WHERE id = ? AND status != ?",
                (TicketStatus.CLOSED, now, now, ticket_id, TicketStatus.CLOSED),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, actor_id, "TICKET_CLOSED", json.dumps({"reason": reason}), now),
            )
            return True

    # ------------------------------------------------------------------
    # flags / monitoring
    # ------------------------------------------------------------------

    async def mark_warned(self, ticket_id: int, stage: str, now: int) -> bool:
        """Record that we warned for this stage. True only on the first warning."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET warned_stage = ?, updated_at = ? "
                "WHERE id = ? AND (warned_stage IS NULL OR warned_stage != ?)",
                (stage, now, ticket_id, stage),
            )
            return cur.rowcount == 1

    async def flag_ticket(self, ticket_id: int, now: int) -> bool:
        """Mark ticket for staff review. True only when it flips 0 -> 1."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET flagged = 1, updated_at = ? WHERE id = ? AND flagged = 0",
                (now, ticket_id),
            )
            return cur.rowcount == 1

    async def set_channel_missing(self, ticket_id: int) -> bool:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE mm_tickets SET channel_missing = 1 WHERE id = ? AND channel_missing = 0",
                (ticket_id,),
            )
            return cur.rowcount == 1

    async def mark_archived(self, ticket_id: int, now: int, mode: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE mm_tickets SET archived = 1, updated_at = ? WHERE id = ?", (now, ticket_id)
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (ticket_id, "TICKET_ARCHIVED", json.dumps({"mode": mode}), now),
            )

    # ------------------------------------------------------------------
    # reports
    # ------------------------------------------------------------------

    async def has_report(self, ticket_id: int, reporter_id: int) -> bool:
        row = await self._fetchone(
            "SELECT id FROM mm_reports WHERE ticket_id = ? AND reporter_id = ? LIMIT 1",
            (ticket_id, reporter_id),
        )
        return row is not None

    async def create_report(
        self,
        *,
        ticket_id: int,
        reporter_id: int,
        reported_mm_id: int,
        category: str,
        description: str,
        prev_ticket_status: str,
        now: int,
    ) -> tuple[Optional[Report], str]:
        """Atomically insert a report with duplicate protection.

        Returns (report, 'created') or (None, 'duplicate').
        """
        async with self._tx() as conn:
            if not config.ALLOW_MULTIPLE_REPORTS:
                async with await conn.execute(
                    "SELECT id FROM mm_reports WHERE ticket_id = ? AND reporter_id = ? LIMIT 1",
                    (ticket_id, reporter_id),
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    return None, "duplicate"

            cur = await conn.execute(
                "INSERT INTO mm_reports (ticket_id, reporter_id, reported_mm_id, category, description, "
                "created_at, status, prev_ticket_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    reporter_id,
                    reported_mm_id,
                    category,
                    description,
                    now,
                    ReportStatus.OPEN,
                    prev_ticket_status,
                ),
            )
            report_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    reporter_id,
                    "REPORT_CREATED",
                    json.dumps(
                        {"report_id": report_id, "category": category, "reported_mm_id": reported_mm_id}
                    ),
                    now,
                ),
            )
            async with await conn.execute(
                "SELECT * FROM mm_reports WHERE id = ?", (report_id,)
            ) as cur2:
                row = await cur2.fetchone()
        return Report.from_row(row), "created"

    async def get_report(self, report_id: int) -> Optional[Report]:
        row = await self._fetchone("SELECT * FROM mm_reports WHERE id = ?", (report_id,))
        return Report.from_row(row) if row else None

    async def attach_report_message(self, report_id: int, channel_id: int, message_id: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE mm_reports SET channel_id = ?, message_id = ? WHERE id = ?",
                (channel_id, message_id, report_id),
            )

    async def set_report_status(
        self, report_id: int, status: str, actor_id: int, now: int
    ) -> Optional[Report]:
        resolved = status == ReportStatus.RESOLVED
        async with self._tx() as conn:
            async with await conn.execute(
                "SELECT * FROM mm_reports WHERE id = ?", (report_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE mm_reports SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
                (status, now if resolved else row["resolved_at"], actor_id if resolved else row["resolved_by"], report_id),
            )
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row["ticket_id"],
                    actor_id,
                    "REPORT_STATUS_CHANGED",
                    json.dumps({"report_id": report_id, "status": status}),
                    now,
                ),
            )
            async with await conn.execute(
                "SELECT * FROM mm_reports WHERE id = ?", (report_id,)
            ) as cur2:
                new_row = await cur2.fetchone()
        return Report.from_row(new_row) if new_row else None

    async def mark_report_delivery_failed(self, report_id: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE mm_reports SET status = ? WHERE id = ?",
                (ReportStatus.DELIVERY_FAILED, report_id),
            )

    async def list_open_reports(self) -> list[Report]:
        rows = await self._fetchall(
            "SELECT * FROM mm_reports WHERE status IN "
            f"({_placeholders(tuple(ReportStatus.OPEN_LIKE))}) ORDER BY id",
            tuple(ReportStatus.OPEN_LIKE),
        )
        return [Report.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # events / audit
    # ------------------------------------------------------------------

    async def add_event(
        self,
        ticket_id: int,
        actor_id: Optional[int],
        event_type: str,
        metadata: Optional[dict] = None,
        now: Optional[int] = None,
    ) -> None:
        now = int(now if now is not None else time.time())
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO mm_events (ticket_id, actor_id, event_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, actor_id, event_type, json.dumps(metadata or {}), now),
            )

    async def get_events(self, ticket_id: int, limit: int = 50) -> list[Event]:
        rows = await self._fetchall(
            "SELECT * FROM mm_events WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
            (ticket_id, limit),
        )
        return [Event.from_row(r) for r in reversed(rows)]

    async def has_event(self, ticket_id: int, event_type: str) -> bool:
        row = await self._fetchone(
            "SELECT id FROM mm_events WHERE ticket_id = ? AND event_type = ? LIMIT 1",
            (ticket_id, event_type),
        )
        return row is not None

    # ------------------------------------------------------------------
    # claims history / stats
    # ------------------------------------------------------------------

    async def get_claims(self, ticket_id: int) -> list[Claim]:
        rows = await self._fetchall(
            "SELECT * FROM mm_claims WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        )
        return [Claim.from_row(r) for r in rows]

    async def count_active_by_creator(self, user_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM mm_tickets WHERE creator_id = ? AND status IN "
            f"({_placeholders(tuple(TicketStatus.ACTIVE))})",
            (user_id, *tuple(TicketStatus.ACTIVE)),
        )
        return int(row["n"]) if row else 0

    async def status_counts(self) -> dict[str, int]:
        rows = await self._fetchall(
            "SELECT status, COUNT(*) AS n FROM mm_tickets GROUP BY status"
        )
        return {r["status"]: int(r["n"]) for r in rows}

    async def open_report_count(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM mm_reports WHERE status IN "
            f"({_placeholders(tuple(ReportStatus.OPEN_LIKE))})",
            tuple(ReportStatus.OPEN_LIKE),
        )
        return int(row["n"]) if row else 0
