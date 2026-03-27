"""MP3 identification entry point.

No imports from tag/name.

Public API:
- Mp3Identifier
- IdentificationPolicy
- IdentificationResult
- TrackId
- TrackMetadata
"""

from .identifier import (
    IdentificationPolicy,
    IdentificationResult,
    Mp3Identifier,
    TrackId,
    TrackMetadata,
)

__all__ = [
    "Mp3Identifier",
    "IdentificationPolicy",
    "IdentificationResult",
    "TrackId",
    "TrackMetadata",
]
