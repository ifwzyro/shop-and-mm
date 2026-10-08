"""Emoji helpers.

All custom emojis come from ``emoji.json``. If an entry is missing, empty or
not a valid custom-emoji string, ``get_emoji`` returns an empty string and the
UI simply shows no emoji. Unicode emojis are never used as replacements.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("mm.emojis")

EMOJI_FILE = Path(__file__).resolve().parent.parent / "emoji.json"

# <name:id> or <a:name:id> with a numeric snowflake id.
_VALID_EMOJI = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{5,30}>$")

_cache: Optional[Dict[str, str]] = None


def _load() -> Dict[str, str]:
    global _cache
    if _cache is None:
        data: Dict[str, str] = {}
        try:
            raw = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): v for k, v in raw.items() if isinstance(v, str)}
            else:
                log.error("emoji.json must contain a JSON object")
        except FileNotFoundError:
            log.warning("emoji.json not found at %s — no emojis will be shown", EMOJI_FILE)
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read emoji.json — no emojis will be shown")
        _cache = data
    return _cache


def reload_emojis() -> None:
    """Force a re-read of emoji.json (used by tests / hot reloads)."""
    global _cache
    _cache = None
    _load()


def get_emoji(name: str) -> str:
    """Return the custom emoji string for *name*, or ``""`` when unavailable.

    Invalid or placeholder values (e.g. ``<CUSTOM_EMOJI>``) are treated as
    missing so nothing broken is ever rendered.
    """
    value = _load().get(name, "")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or not _VALID_EMOJI.match(value):
        return ""
    return value


def button_emoji(name: str) -> Optional[str]:
    """Like :func:`get_emoji` but returns ``None`` for component parameters."""
    return get_emoji(name) or None


def with_emoji(name: str, text: str) -> str:
    """Prefix *text* with the emoji for *name* when one exists."""
    emoji = get_emoji(name)
    return f"{emoji} {text}".strip() if emoji else text
