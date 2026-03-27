"""VirtualDJ .m3u history processing (local-only parsing + optional Drive helpers).

Recommended entry point:
- M3UToolbox

Backwards compatible functions are also exported for existing callers.
"""

from .m3u import M3UEntry, M3UToolbox

__all__ = [
    "M3UToolbox",
    "M3UEntry",
]
