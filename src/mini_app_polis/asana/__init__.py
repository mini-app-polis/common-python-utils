"""Asana API client for the MiniAppPolis ecosystem.

    from mini_app_polis.asana import AsanaClient, AsanaTaskInput

The client is source-agnostic on purpose. Voice notes are the first
caller; Discord-sourced tasks and conformance findings are the ones
anticipated. What each of them supplies is an ``external_id`` of its
own namespacing, so the shared idempotency check works the same way
for all of them without this package knowing any of them exist.
"""

from .client import ASANA_API_BASE, AsanaClient
from .errors import AsanaAPIError, AsanaAuthError, AsanaError
from .rich_text import escape_rich_text, link, rich_text_body
from .types import AsanaTaskInput

__all__ = [
    "ASANA_API_BASE",
    "AsanaAPIError",
    "AsanaAuthError",
    "AsanaClient",
    "AsanaError",
    "AsanaTaskInput",
    "escape_rich_text",
    "link",
    "rich_text_body",
]
