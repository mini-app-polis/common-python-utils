"""MP3 tagging entry point.

No imports from identify/name.

Public API:
- Mp3Tagger
- TagSnapshot
- AudioTagger, for every supported audio format (AudioFormat), with
  UnsupportedFile / UnsupportedReason for a file it cannot tag
"""

from .audio_tagger import SUPPORTED_FORMATS, AudioTagger
from .models import AudioFormat, UnsupportedFile, UnsupportedReason
from .tagger import Mp3Tagger, TagSnapshot

__all__ = [
    "Mp3Tagger",
    "TagSnapshot",
    "AudioTagger",
    "AudioFormat",
    "SUPPORTED_FORMATS",
    "UnsupportedFile",
    "UnsupportedReason",
]
