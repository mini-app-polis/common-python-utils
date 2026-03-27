"""MP3 utilities.

Three independent entry points (no crossover):
- identify_audio.mp3.identify
- identify_audio.mp3.tag
- identify_audio.mp3.name

Convenience exports are provided but optional.
"""

from .identify import (
    IdentificationPolicy,
    IdentificationResult,
    Mp3Identifier,
    TrackId,
    TrackMetadata,
)
from .rename import Mp3Renamer
from .tag import Mp3Tagger, TagSnapshot

__all__ = [
    "Mp3Identifier",
    "IdentificationPolicy",
    "IdentificationResult",
    "TrackId",
    "TrackMetadata",
    "Mp3Tagger",
    "TagSnapshot",
    "Mp3Renamer",
]
