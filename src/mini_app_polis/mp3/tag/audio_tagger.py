"""Tag reading and writing for every audio format the tagging stack supports.

The same fields and the same snapshot as :class:`Mp3Tagger`, for MP3, FLAC,
Ogg Vorbis, Ogg Opus, M4A (MP4 audio) and the ID3 chunk of WAV and AIFF.
The format is taken from the file's contents, not its name, and a file is
never converted: only its tag block is rewritten. A file it cannot tag gets
an :class:`UnsupportedFile` back instead of an exception, and is not written.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .io.music_tag_io import MusicTagIO
from .models import AudioFormat, TagSnapshot, UnsupportedFile, UnsupportedReason

mutagen: Any | None
_mutagen_err: Exception | None
try:
    import mutagen as _mutagen
    import mutagen.aiff
    import mutagen.flac
    import mutagen.mp3
    import mutagen.mp4
    import mutagen.oggopus
    import mutagen.oggvorbis
    import mutagen.wave
except Exception as e:  # pragma: no cover
    mutagen = None
    _mutagen_err = e
    _FORMATS: tuple[tuple[Any, AudioFormat], ...] = ()
else:
    mutagen = _mutagen
    _mutagen_err = None
    _FORMATS = (
        (mutagen.mp3.MP3, AudioFormat.MP3),
        (mutagen.flac.FLAC, AudioFormat.FLAC),
        (mutagen.oggvorbis.OggVorbis, AudioFormat.OGG_VORBIS),
        (mutagen.oggopus.OggOpus, AudioFormat.OGG_OPUS),
        (mutagen.mp4.MP4, AudioFormat.M4A),
        (mutagen.wave.WAVE, AudioFormat.WAV),
        (mutagen.aiff.AIFF, AudioFormat.AIFF),
    )

#: The formats AudioTagger reads and writes.
SUPPORTED_FORMATS: frozenset[AudioFormat] = frozenset(AudioFormat)

#: Name endings of downloads still in progress.
PARTIAL_DOWNLOAD_SUFFIXES = (".part",)


class AudioTagger:
    """Read/write tags on local audio files in any supported format.

    Contract:
    - detect() returns the file's AudioFormat, or UnsupportedFile
    - read() returns TagSnapshot (tags are strings), or UnsupportedFile
    - write() accepts the same metadata mapping as Mp3Tagger.write() and
      returns the AudioFormat it wrote, or UnsupportedFile without writing
    A missing path raises FileNotFoundError.
    """

    def __init__(self, io: MusicTagIO | None = None):
        self._io = io or MusicTagIO()

    def detect(self, path: str) -> AudioFormat | UnsupportedFile:
        """Return the format of the file at ``path`` from its contents."""
        if path.lower().endswith(PARTIAL_DOWNLOAD_SUFFIXES):
            return UnsupportedFile(path, UnsupportedReason.PARTIAL_DOWNLOAD)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if mutagen is None:  # pragma: no cover
            raise ImportError(
                "mutagen is required for tagging. Install the mp3 extra to use AudioTagger."
            ) from _mutagen_err
        try:
            mfile = mutagen.File(path)
        except mutagen.MutagenError as e:
            return UnsupportedFile(path, UnsupportedReason.UNREADABLE, repr(e))
        if mfile is None:
            return UnsupportedFile(path, UnsupportedReason.NOT_AUDIO)
        for kls, fmt in _FORMATS:
            if isinstance(mfile, kls):
                return fmt
        return UnsupportedFile(
            path, UnsupportedReason.UNSUPPORTED_FORMAT, type(mfile).__name__
        )

    def read(self, path: str) -> TagSnapshot | UnsupportedFile:
        """Read tags from a local file into a TagSnapshot."""
        fmt = self.detect(path)
        if isinstance(fmt, UnsupportedFile):
            return fmt
        return self._io.read(path)

    def write(
        self,
        path: str,
        metadata: Mapping[str, Any],
        *,
        ensure_virtualdj_compat: bool = False,
    ) -> AudioFormat | UnsupportedFile:
        """Write metadata tags to a local file; the audio is left as it is.

        ``ensure_virtualdj_compat`` applies to MP3 files only, as in
        Mp3Tagger.write().
        """
        fmt = self.detect(path)
        if isinstance(fmt, UnsupportedFile):
            return fmt
        self._io.write(path, metadata, ensure_virtualdj_compat=ensure_virtualdj_compat)
        return fmt
