"""Report to Sentry when the host process has it, and do nothing when it does not.

``sentry_sdk`` is not a dependency of this library: the cogs that only use
the Google or mp3 helpers should not start paying for one. Every service
that does want reports already depends on it and calls ``sentry_sdk.init``
itself. Both absences — not installed, installed but never initialised —
no-op cleanly, so these are safe to call from anywhere.

The module is looked up once, at import, and held as ``sentry_sdk`` so a
test can replace it, and so the lookup is not repeated on every failure.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any


def _load() -> Any:
    try:
        return importlib.import_module("sentry_sdk")
    except ImportError:
        return None


#: The ``sentry_sdk`` module, or None when it is not installed.
sentry_sdk: Any = _load()


def capture_exception(exc: BaseException) -> str | None:
    """Report ``exc``; return the event id, or None when nothing was sent."""
    if sentry_sdk is None:
        return None
    try:
        event_id = sentry_sdk.capture_exception(exc)
    except Exception:  # reporting a failure must not become a second one
        return None
    return str(event_id) if event_id else None


def capture_message(message: str, *, level: str = "error") -> None:
    """Report ``message`` at ``level``. Never raises."""
    if sentry_sdk is None:
        return
    with contextlib.suppress(Exception):
        sentry_sdk.capture_message(message, level=level)
