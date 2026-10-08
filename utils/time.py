"""Time helpers. All persisted timestamps are unix seconds (UTC)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional


def now() -> int:
    """Current unix timestamp in seconds."""
    return int(time.time())


def to_datetime(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def format_ts(ts: Optional[int], style: str = "F") -> str:
    """Discord timestamp markup, rendered in each viewer's local timezone.

    Styles: 't' short time, 'T' long time, 'd' short date, 'D' long date,
    'f' date+time (default long), 'F' full date+time, 'R' relative.
    Returns a readable fallback when *ts* is missing.
    """
    if ts is None:
        return "—"
    return f"<t:{int(ts)}:{style}>"


def format_duration(seconds: int) -> str:
    """Humanise a duration, e.g. ``1h 30m``, ``45s``."""
    seconds = max(0, int(seconds))
    parts: list[str] = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days:
        parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"


def remaining(deadline: Optional[int], reference: Optional[int] = None) -> str:
    """Humanised time left until *deadline* (or ``—``)."""
    if deadline is None:
        return "—"
    reference = reference if reference is not None else now()
    delta = deadline - reference
    if delta <= 0:
        return "expired"
    return format_duration(delta)


def clock(ts: Optional[int]) -> str:
    """``HH:MM:SS`` UTC for audit timelines."""
    if ts is None:
        return "--:--:--"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def short_date(ts: Optional[int]) -> str:
    """``8 Oct 2026, 17:30`` UTC — plain-text variant for embeds/files."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y, %H:%M")


def discord_file_ts(ts: Optional[int]) -> Optional[datetime]:
    return to_datetime(ts)
