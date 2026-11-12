"""MP3 tagging entry point.

No imports from identify/name.

Public API:
- Mp3Tagger
- TagSnapshot
- AudioTagger, for every supported audio format (AudioFormat), with
  UnsupportedFile / UnsupportedReason for a file it cannot tag
- RuleProfile (the main and fx profiles) and apply_profile, which compares a
  file with its catalog values under a profile and writes or reports
"""

from .audio_tagger import SUPPORTED_FORMATS, AudioTagger
from .models import AudioFormat, UnsupportedFile, UnsupportedReason
from .profiles import (
    FX,
    MAIN,
    PROFILE_FIELDS,
    PROFILES,
    FieldChange,
    ProfileResult,
    RuleProfile,
    Winner,
    apply_profile,
    get_profile,
)
from .tagger import Mp3Tagger, TagSnapshot

__all__ = [
    "Mp3Tagger",
    "TagSnapshot",
    "AudioTagger",
    "AudioFormat",
    "SUPPORTED_FORMATS",
    "UnsupportedFile",
    "UnsupportedReason",
    "RuleProfile",
    "Winner",
    "MAIN",
    "FX",
    "PROFILES",
    "PROFILE_FIELDS",
    "FieldChange",
    "ProfileResult",
    "apply_profile",
    "get_profile",
]
