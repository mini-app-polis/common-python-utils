"""MP3 naming (renaming) entry point.

No imports from identify/tag.

Public API:
- Mp3Renamer
- RenameProposal
- RenameResult
"""

from .renamer import Mp3Renamer

__all__ = ["Mp3Renamer"]
