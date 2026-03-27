"""MP3 tagging entry point.

No imports from identify/name.

Public API:
- Mp3Tagger
- TagSnapshot
"""

from .tagger import Mp3Tagger, TagSnapshot

__all__ = ["Mp3Tagger", "TagSnapshot"]
