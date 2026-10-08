"""Dataclasses and constants describing persisted MM bot entities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import config


class TicketStatus:
    """All ticket states. State is ALWAYS read from the database, never inferred."""

    CREATED = "CREATED"
    WAITING_FOR_PARTNER = "WAITING_FOR_PARTNER"
    PARTNER_ADDED = "PARTNER_ADDED"
    WAITING_FOR_ROLES = "WAITING_FOR_ROLES"
    ROLES_SELECTED = "ROLES_SELECTED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    WAITING_FOR_MM = "WAITING_FOR_MM"
    MM_CLAIMED = "MM_CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    DISPUTED = "DISPUTED"
    CLOSED = "CLOSED"

    ALL = frozenset(
        {
            CREATED,
            WAITING_FOR_PARTNER,
            PARTNER_ADDED,
            WAITING_FOR_ROLES,
            ROLES_SELECTED,
            WAITING_FOR_CONFIRMATION,
            CONFIRMED,
            WAITING_FOR_MM,
            MM_CLAIMED,
            IN_PROGRESS,
            COMPLETED,
            CANCELLED,
            DISPUTED,
            CLOSED,
        }
    )
    TERMINAL = frozenset({COMPLETED, CANCELLED, CLOSED})
    ACTIVE = ALL - TERMINAL
    # States in which an MM is (or just was) assigned to the ticket.
    CLAIMED = frozenset({MM_CLAIMED, IN_PROGRESS, DISPUTED})
    # States in which the ticket is waiting on traders to act.
    AWAITING_TRADERS = frozenset(
        {
            CREATED,
            WAITING_FOR_PARTNER,
            PARTNER_ADDED,
            WAITING_FOR_ROLES,
            ROLES_SELECTED,
            WAITING_FOR_CONFIRMATION,
            CONFIRMED,
        }
    )


class TradeRole:
    BUYER = "BUYER"
    SELLER = "SELLER"
    VALID = frozenset({BUYER, SELLER})


class ReportStatus:
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    DELIVERY_FAILED = "DELIVERY_FAILED"

    OPEN_LIKE = frozenset({OPEN, ACKNOWLEDGED, ESCALATED, DELIVERY_FAILED})


class EventType:
    TICKET_CREATED = "TICKET_CREATED"
    PARTNER_ADDED = "PARTNER_ADDED"
    ROLE_SELECTED = "ROLE_SELECTED"
    ROLE_CHANGED = "ROLE_CHANGED"
    CONFIRMATION_STARTED = "CONFIRMATION_STARTED"
    TRADER_CONFIRMED = "TRADER_CONFIRMED"
    TRADER_DECLINED = "TRADER_DECLINED"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    MM_REQUESTED = "MM_REQUESTED"
    MM_CLAIMED = "MM_CLAIMED"
    MM_RELEASED = "MM_RELEASED"
    MM_REASSIGNED = "MM_REASSIGNED"
    REPORT_CREATED = "REPORT_CREATED"
    REPORT_STATUS_CHANGED = "REPORT_STATUS_CHANGED"
    TICKET_COMPLETED = "TICKET_COMPLETED"
    TICKET_CANCELLED = "TICKET_CANCELLED"
    TICKET_CLOSED = "TICKET_CLOSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_DENIED = "CANCEL_DENIED"
    TIMEOUT_WARNING = "TIMEOUT_WARNING"
    TICKET_FLAGGED = "TICKET_FLAGGED"
    CHANNEL_MISSING = "CHANNEL_MISSING"
    MM_ROLE_LOST = "MM_ROLE_LOST"
    TICKET_ARCHIVED = "TICKET_ARCHIVED"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


@dataclass
class Ticket:
    id: int
    ticket_number: int
    guild_id: int
    channel_id: Optional[int]
    creator_id: int
    partner_id: Optional[int]
    status: str
    created_at: int
    updated_at: int
    closed_at: Optional[int]
    claimed_mm_id: Optional[int]
    claimed_at: Optional[int]
    partner_added_at: Optional[int]
    confirm_started_at: Optional[int]
    confirm_expires_at: Optional[int]
    mm_requested_at: Optional[int]
    last_activity_at: int
    stage_started_at: int
    warned_stage: Optional[str]
    flagged: int
    channel_missing: int
    archived: int
    cancel_reason: Optional[str]
    status_message_id: Optional[int]
    confirm_message_id: Optional[int]
    mm_message_id: Optional[int]

    @property
    def label(self) -> str:
        """Permanent human ticket code, e.g. MM-0042 (never derived from channel name)."""
        return f"{config.TICKET_PREFIX.upper()}-{self.ticket_number:0{config.TICKET_NUMBER_PADDING}d}"

    @property
    def is_active(self) -> bool:
        return self.status in TicketStatus.ACTIVE

    @property
    def has_mm(self) -> bool:
        return self.claimed_mm_id is not None

    @classmethod
    def from_row(cls, row: Any) -> "Ticket":
        return cls(
            id=row["id"],
            ticket_number=row["ticket_number"],
            guild_id=row["guild_id"],
            channel_id=_row_get(row, "channel_id"),
            creator_id=row["creator_id"],
            partner_id=_row_get(row, "partner_id"),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=_row_get(row, "closed_at"),
            claimed_mm_id=_row_get(row, "claimed_mm_id"),
            claimed_at=_row_get(row, "claimed_at"),
            partner_added_at=_row_get(row, "partner_added_at"),
            confirm_started_at=_row_get(row, "confirm_started_at"),
            confirm_expires_at=_row_get(row, "confirm_expires_at"),
            mm_requested_at=_row_get(row, "mm_requested_at"),
            last_activity_at=_row_get(row, "last_activity_at", row["created_at"]),
            stage_started_at=_row_get(row, "stage_started_at", row["created_at"]),
            warned_stage=_row_get(row, "warned_stage"),
            flagged=_row_get(row, "flagged", 0),
            channel_missing=_row_get(row, "channel_missing", 0),
            archived=_row_get(row, "archived", 0),
            cancel_reason=_row_get(row, "cancel_reason"),
            status_message_id=_row_get(row, "status_message_id"),
            confirm_message_id=_row_get(row, "confirm_message_id"),
            mm_message_id=_row_get(row, "mm_message_id"),
        )


@dataclass
class Trader:
    id: int
    ticket_id: int
    user_id: int
    trade_role: Optional[str]
    confirmed: bool
    confirmed_at: Optional[int]

    @classmethod
    def from_row(cls, row: Any) -> "Trader":
        return cls(
            id=row["id"],
            ticket_id=row["ticket_id"],
            user_id=row["user_id"],
            trade_role=_row_get(row, "trade_role"),
            confirmed=bool(row["confirmed"]),
            confirmed_at=_row_get(row, "confirmed_at"),
        )


@dataclass
class Claim:
    id: int
    ticket_id: int
    mm_id: int
    claimed_at: int
    released_at: Optional[int]

    @classmethod
    def from_row(cls, row: Any) -> "Claim":
        return cls(
            id=row["id"],
            ticket_id=row["ticket_id"],
            mm_id=row["mm_id"],
            claimed_at=row["claimed_at"],
            released_at=_row_get(row, "released_at"),
        )


@dataclass
class Report:
    id: int
    ticket_id: int
    reporter_id: int
    reported_mm_id: int
    category: str
    description: str
    created_at: int
    status: str
    channel_id: Optional[int]
    message_id: Optional[int]
    resolved_at: Optional[int]
    resolved_by: Optional[int]
    prev_ticket_status: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "Report":
        return cls(
            id=row["id"],
            ticket_id=row["ticket_id"],
            reporter_id=row["reporter_id"],
            reported_mm_id=row["reported_mm_id"],
            category=row["category"],
            description=row["description"],
            created_at=row["created_at"],
            status=row["status"],
            channel_id=_row_get(row, "channel_id"),
            message_id=_row_get(row, "message_id"),
            resolved_at=_row_get(row, "resolved_at"),
            resolved_by=_row_get(row, "resolved_by"),
            prev_ticket_status=_row_get(row, "prev_ticket_status"),
        )


@dataclass
class Event:
    id: int
    ticket_id: int
    actor_id: Optional[int]
    event_type: str
    metadata: dict
    created_at: int

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        raw = _row_get(row, "metadata") or "{}"
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            meta = {}
        return cls(
            id=row["id"],
            ticket_id=row["ticket_id"],
            actor_id=_row_get(row, "actor_id"),
            event_type=row["event_type"],
            metadata=meta if isinstance(meta, dict) else {},
            created_at=row["created_at"],
        )
