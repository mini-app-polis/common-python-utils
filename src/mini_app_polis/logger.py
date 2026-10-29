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
``LOGGING_LEVEL`` sets the level; the default is ``INFO``, not ``DEBUG``.
Importing this module calls :func:`logging.basicConfig`, which configures
the **root** logger, so the level applies to every third-party library in
the process — not just to ``mini_app_polis``. A DEBUG default would run
every dependency at DEBUG in any consumer that has not set
``LOGGING_LEVEL``, and ``websockets`` logs its full request headers at
DEBUG, including ``Authorization: bearer <key>``, and then logs the auth
frame body as well — a credential in plaintext in the service's logs.

The INFO default is necessary but not sufficient — anyone raising
``LOGGING_LEVEL`` to DEBUG to debug their own cog would reopen the leak.
So the loggers known to print credentials are held at a floor
(:data:`_CREDENTIAL_LOGGER_FLOOR`) regardless of ``LOGGING_LEVEL``.
Debugging your own code must never print your credentials.

The floor keeps header dumps out, but not a secret carried in a URL's path:
httpx's INFO line ``HTTP Request: POST <url> …`` prints the full URL, and a
Discord webhook URL is ``/api/webhooks/<id>/<token>``, so without this live
webhook tokens reach a service's logs. A
filter on the same loggers replaces those secrets with ``<redacted>`` and
keeps the line (:data:`_URL_SECRET_PATTERNS`).

If you genuinely need that transport-level output, opt in explicitly and
temporarily in your own process::

    logging.getLogger("websockets").setLevel(logging.DEBUG)

Output format
-------------
On Railway, each line is one JSON object written to **stdout**:
``{"message", "level", "logger", "source", "function"}``. Railway reads
``level`` (``debug``/``info``/``warn``/``error``) as the line's severity and
turns every other key into an ``@name:value`` filter. Before this, the root
handler wrote plain text to stderr, which Railway files as ``error``, so
every line from every service in the fleet showed as an error (found on
db-poller, 2026-10-16).

Everywhere else the format is unchanged: plain text on stderr, which keeps
stdout clean for scripts that print data. Railway is detected from its own
environment variables (:data:`_RAILWAY_ENV_VARS`); ``LOG_FORMAT=json`` or
``LOG_FORMAT=text`` overrides the detection either way.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys

# No load_dotenv(). Secrets and settings come from the process environment
# only: Doppler (`doppler run` locally, SSM or the platform sync deployed).
# Loading a .env file at import would make every consumer of the library
# read one wherever it happened to sit, and hide where a value came from.

# Ecosystem log prefix conventions
LOG_START = "🚀"
LOG_SUCCESS = "✅"
LOG_FAILURE = "❌"
LOG_WARNING = "⚠️"

#: Default is INFO, not DEBUG — see the module docstring. ``basicConfig``
#: below configures the root logger, so this level reaches every library
#: in the process, and DEBUG there prints credentials.
_level = os.getenv("LOGGING_LEVEL", "INFO").upper()

_TEXT_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d - %(funcName)s] %(message)s"
)
_TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Variables Railway sets in every deployment. Any one present means the
#: process is running on Railway and its logs are read by Railway's parser.
_RAILWAY_ENV_VARS: tuple[str, ...] = (
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
)


def _use_json() -> bool:
    """True when log lines should be Railway structured JSON.

    ``LOG_FORMAT`` (``json`` or ``text``) decides when set; otherwise JSON
    exactly when running on Railway.
    """
    override = (os.getenv("LOG_FORMAT") or "").strip().lower()
    if override in ("json", "text"):
        return override == "json"
    return any(os.getenv(name) for name in _RAILWAY_ENV_VARS)


def _railway_level(levelno: int) -> str:
    """Map a logging level to the four severities Railway recognises."""
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class RailwayJsonFormatter(logging.Formatter):
    """Format a record as one line of JSON in Railway's structured-log shape.

    ``message`` and ``level`` are the two keys Railway interprets; the rest
    become filterable attributes. A traceback is appended to ``message``
    rather than split across lines, because Railway treats every physical
    line as a separate log entry.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single-line JSON object."""
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        payload = {
            "message": message,
            "level": _railway_level(record.levelno),
            "logger": record.name,
            "source": f"{record.filename}:{record.lineno}",
            "function": record.funcName,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def _root_handler() -> logging.Handler:
    """The handler ``basicConfig`` installs on root: JSON to stdout on
    Railway, the established text format to stderr everywhere else."""
    if _use_json():
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(RailwayJsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt=_TEXT_DATEFMT))
    return handler


# Still a no-op when root already has a handler (AWS Lambda installs one),
# so hosts that configured logging themselves keep their own format.
logging.basicConfig(level=_level, handlers=[_root_handler()])

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

#: Secrets that travel in a URL's path, where the floor above cannot keep
#: them out of an INFO line. Each pattern's first group is kept and the rest
#: of the match is replaced. Add a pattern here for any other service that
#: puts a credential in a URL.
#:
#: * Discord: the webhook token, ``/api/webhooks/<id>/<token>`` — the
#:   observed leak.
#: * Healthchecks.io: the check's UUID, or the project ping key in the
#:   ``/<ping-key>/<slug>`` form, is the whole credential — anyone holding
#:   the URL can mark the check up or down, which is exactly the silence a
#:   dead-man's switch exists to catch. The ``/start``, ``/fail`` or slug
#:   after it is kept.
_URL_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(/api/webhooks/\d+/)[^/\s\"'?#]+"),
    re.compile(r"(hc-ping\.com/)[^/\s\"'?#]+"),
)

_REDACTED = "<redacted>"


class _UrlSecretRedactor(logging.Filter):
    """Replace URL-borne secrets in a record's message; never drop the record.

    The line is kept because it is often the only record that a request went
    out. Only the secret goes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place when its message carries a secret."""
        message = record.getMessage()
        redacted = message
        for pattern in _URL_SECRET_PATTERNS:
            redacted = pattern.sub(rf"\g<1>{_REDACTED}", redacted)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def _install_url_secret_redaction() -> None:
    """Attach the redactor to each credential-bearing logger, once.

    Matched by class name rather than identity, so reloading this module
    (the tests do) does not stack a second filter on each logger.
    """
    for name in _CREDENTIAL_BEARING_LOGGERS:
        logger = logging.getLogger(name)
        if not any(
            type(f).__name__ == _UrlSecretRedactor.__name__ for f in logger.filters
        ):
            logger.addFilter(_UrlSecretRedactor())


_install_url_secret_redaction()

_logger = logging.getLogger("mini_app_polis")

# Set on this logger too, not only through basicConfig. basicConfig does
# nothing when the root logger already has a handler — which on AWS Lambda
# it always does, installed by the runtime at WARNING — so there ``_level``
# never applied and every INFO line a cog wrote was dropped, while the
# credential-bearing loggers above, whose levels are set explicitly, still
# printed. Setting it here reaches the fleet's own logging without touching
# the root level, so third-party libraries keep whatever the host chose.
_logger.setLevel(_level)


def apply_level() -> str:
    """Re-read ``LOGGING_LEVEL`` and apply it to the fleet's logger.

    The level is read at import, which on a Lambda worker is once per cold
    start — so changing ``LOGGING_LEVEL`` in Doppler did nothing until a
    deploy. A worker that refreshes its settings per invocation
    (``load_secrets(refresh=True)``) calls this afterwards, and the new
    level applies from that run.

    Sets the ``mini_app_polis`` logger, as at import, and leaves root alone.
    The credential-bearing loggers stay at their floor whatever the level.
    An unrecognised value is logged and ignored: a typo in a setting must
    not take the worker down or silence it. Returns the level in effect.
    """
    global _level
    requested = (os.getenv("LOGGING_LEVEL") or "INFO").strip().upper()
    if requested not in logging.getLevelNamesMapping():
        _logger.warning(
            "LOGGING_LEVEL=%r is not a logging level; keeping %s", requested, _level
        )
        return _level
    if requested != _level:
        _logger.info("logging level %s -> %s", _level, requested)
        _level = requested
    _logger.setLevel(_level)
    _clamp_credential_bearing_loggers()
    return _level


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
