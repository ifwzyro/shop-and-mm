"""Logging setup.

Console output stays clean: meaningful events only (startup, database,
cog loading, errors, moderation-critical actions). Normal button clicks are
deliberately not logged.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

import config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if config.DEBUG else logging.INFO)

    # Reset handlers so repeated calls (tests) do not duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if config.DEBUG else logging.INFO)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(console)

    try:
        log_path = Path(config.LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(file_handler)
    except OSError:
        root.warning("Could not open log file %s — file logging disabled", config.LOG_FILE)

    # Third-party noise: quiet unless debugging.
    logging.getLogger("discord").setLevel(logging.DEBUG if config.DEBUG else logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.DEBUG if config.DEBUG else logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    return logging.getLogger("mm")


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name or "mm")
