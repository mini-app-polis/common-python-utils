"""Kaiano logging setup and ecosystem log conventions.

Key lifecycle log lines (info/warning/error) should use a leading emoji so
operators can scan logs quickly:

- ``LOG_START`` — pipeline or long-running job started
- ``LOG_SUCCESS`` — completed successfully
- ``LOG_FAILURE`` — terminal failure
- ``LOG_WARNING`` — recoverable issue or degraded path

Use :func:`with_log_prefix` when building messages so spacing stays consistent.
"""

from __future__ import annotations

import datetime
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# Ecosystem log prefix conventions
LOG_START = "🚀"
LOG_SUCCESS = "✅"
LOG_FAILURE = "❌"
LOG_WARNING = "⚠️"

_level = os.getenv("LOGGING_LEVEL", "DEBUG").upper()

logging.basicConfig(
    level=_level,
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d - %(funcName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_logger = logging.getLogger("kaiano")

# Shortcut aliases — used across consumer repos
debug = _logger.debug
info = _logger.info
warning = _logger.warning
error = _logger.error
exception = _logger.exception


def get_logger() -> logging.Logger:
    return _logger


def with_log_prefix(emoji: str, message: str) -> str:
    """Return ``message`` with one leading emoji and a single space after it."""
    clean_message = " ".join(str(message).split())
    return f"{emoji.strip()} {clean_message}".strip()


def format_date(dt: datetime.datetime) -> str:
    """Format a datetime to a human-readable string. YYYY-MM-DD HH:MM."""
    return dt.strftime("%Y-%m-%d %H:%M")
