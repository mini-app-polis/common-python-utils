from __future__ import annotations

import datetime
import logging
import os

from dotenv import load_dotenv

load_dotenv()

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


def format_date(dt: datetime.datetime) -> str:
    """Format a datetime to a human-readable string. YYYY-MM-DD HH:MM."""
    return dt.strftime("%Y-%m-%d %H:%M")
