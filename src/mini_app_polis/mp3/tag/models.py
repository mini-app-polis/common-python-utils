from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True)
class TagSnapshot:
    """Hold normalized tag values and artwork presence for one file."""

    tags: dict[str, str] = field(default_factory=dict)
    has_artwork: bool = False


class AudioFormat(StrEnum):
    """An audio format AudioTagger reads and writes tags in."""

    MP3 = "mp3"
    FLAC = "flac"
    OGG_VORBIS = "ogg_vorbis"
    OGG_OPUS = "ogg_opus"
    M4A = "m4a"
    WAV = "wav"
    AIFF = "aiff"


class UnsupportedReason(StrEnum):
    """Why AudioTagger left a file alone."""

    #: A download still in progress (``.part``); not opened at all.
    PARTIAL_DOWNLOAD = "partial_download"
    #: Not recognised as audio (a document, a spreadsheet, an image).
    NOT_AUDIO = "not_audio"
    #: Looks like an audio format but could not be parsed (empty, truncated,
    #: or another kind of file under an audio extension).
    UNREADABLE = "unreadable"
    #: Audio, in a format AudioTagger does not tag.
    UNSUPPORTED_FORMAT = "unsupported_format"


@dataclass(frozen=True)
class UnsupportedFile:
    """AudioTagger's answer for a file it cannot tag. The file is untouched."""

    path: str
    reason: UnsupportedReason
    #: What was found, for a log line: the parser's error or the format name.
    detail: str = ""
