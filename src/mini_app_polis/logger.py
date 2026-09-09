"""Kaiano logging setup and ecosystem log conventions.

Key lifecycle log lines (info/warning/error) should use a leading emoji so
operators can scan logs quickly:

- ``LOG_START`` — pipeline or long-running job started
- ``LOG_SUCCESS`` — completed successfully
- ``LOG_FAILURE`` — terminal failure
- ``LOG_WARNING`` — recoverable issue or degraded path

Use :func:`with_log_prefix` when building messages so spacing stays consistent.

Log level
---------
``LOGGING_LEVEL`` sets the level; the default is ``INFO``. It used to be
``DEBUG``, which was a mistake with real consequences: importing this
module calls :func:`logging.basicConfig`, which configures the **root**
logger, so the level applies to every third-party library in the process
— not just to ``mini_app_polis``. Any consumer that had not set
``LOGGING_LEVEL`` therefore ran every dependency at DEBUG.

That is how a Prefect API key ended up in plaintext in Railway logs on
2026-09-09: ``websockets`` logs its full request headers at DEBUG,
including ``Authorization: bearer <key>``, and then logs the auth frame
body as well.

Changing the default is necessary but not sufficient — anyone raising
``LOGGING_LEVEL`` to DEBUG to debug their own cog would reopen the leak.
So the loggers known to print credentials are held at a floor
(:data:`_CREDENTIAL_LOGGER_FLOOR`) regardless of ``LOGGING_LEVEL``.
Debugging your own code must never print your credentials.

If you genuinely need that transport-level output, opt in explicitly and
temporarily in your own process::

    logging.getLogger("websockets").setLevel(logging.DEBUG)
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

#: Default is INFO, not DEBUG — see the module docstring. ``basicConfig``
#: below configures the root logger, so this level reaches every library
#: in the process, and DEBUG there prints credentials.
_level = os.getenv("LOGGING_LEVEL", "INFO").upper()

logging.basicConfig(
    level=_level,
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d - %(funcName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

#: Third-party loggers that print credentials or full request headers at
#: DEBUG. ``websockets`` is the observed leak — Prefect's event client
#: uses it, and it logged ``Authorization: bearer <PREFECT_API_KEY>``
#: plus the auth frame on every container start. The HTTP transports are
#: the same class of risk: they log request headers, which carry bearer
#: tokens and API keys for every service in the fleet.
#:
#: Names are logger prefixes — setting ``websockets`` covers
#: ``websockets.client`` and every other child.
_CREDENTIAL_BEARING_LOGGERS: tuple[str, ...] = (
    "websockets",
    "httpcore",
    "httpx",
    "urllib3",
    "google_auth_httplib2",
    "googleapiclient.discovery",
)

#: The floor those loggers are held at. INFO keeps their useful lines
#: (``HTTP Request: POST … 200 OK``) while dropping the header dumps.
_CREDENTIAL_LOGGER_FLOOR = logging.INFO


def _clamp_credential_bearing_loggers() -> None:
    """Hold credential-printing third-party loggers at or above the floor.

    Applied unconditionally at import, and deliberately not overridable
    by ``LOGGING_LEVEL``: raising the level to debug your own code should
    never start printing your secrets. Only levels *below* the floor are
    raised, so a consumer that has already set one of these to WARNING
    keeps its quieter setting.
    """
    for name in _CREDENTIAL_BEARING_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < _CREDENTIAL_LOGGER_FLOOR:
            logger.setLevel(_CREDENTIAL_LOGGER_FLOOR)


_clamp_credential_bearing_loggers()

_logger = logging.getLogger("mini_app_polis")

# Shortcut aliases — used across consumer repos
debug = _logger.debug
info = _logger.info
warning = _logger.warning
error = _logger.error
exception = _logger.exception


def get_logger() -> logging.Logger:
    """Return the shared mini_app_polis logger instance."""
    return _logger


def with_log_prefix(emoji: str, message: str) -> str:
    """Return ``message`` with one leading emoji and a single space after it."""
    clean_message = " ".join(str(message).split())
    return f"{emoji.strip()} {clean_message}".strip()


def format_date(dt: datetime.datetime) -> str:
    """Format a datetime to a human-readable string. YYYY-MM-DD HH:MM."""
    return dt.strftime("%Y-%m-%d %H:%M")
