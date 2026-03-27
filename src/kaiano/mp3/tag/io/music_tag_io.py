from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from typing import Any

try:
    import music_tag  # type: ignore
except Exception as e:  # pragma: no cover
    music_tag = None
    _music_tag_err = e
else:
    _music_tag_err = None

try:
    from mutagen.id3 import ID3, TDRC, TYER, ID3NoHeaderError  # type: ignore
except Exception as e:  # pragma: no cover
    ID3 = TDRC = TYER = ID3NoHeaderError = None  # type: ignore
    _mutagen_err = e
else:
    _mutagen_err = None

try:
    import kaiano.logger as log  # type: ignore
except Exception:  # pragma: no cover
    import logging

    log = logging.getLogger(__name__)

from ..models import TagSnapshot

# Curated keys for debug dumping
TAG_FIELDS = [
    "tracktitle",
    "artist",
    "album",
    "albumartist",
    "year",
    "date",
    "genre",
    "bpm",
    "comment",
    "isrc",
    "tracknumber",
    "discnumber",
]


class MusicTagIO:
    """Adapter around the `music_tag` library.

    This module is *only* about reading/writing tags on local files.
    """

    def _normalize_year_for_tag(self, v: str | None) -> str:
        s = "" if v is None else str(v).strip()
        if not s:
            return ""
        if len(s) >= 4 and s[:4].isdigit():
            return s[:4]
        return ""

    def _save_virtualdj_id3_compat(self, path: str, year: str | None) -> None:
        """Best-effort: ensure VirtualDJ-friendly ID3v2.3 save (mp3 only)."""
        try:
            if ID3 is None:  # pragma: no cover
                return
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext != "mp3":
                return

            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                id3 = ID3()

            normalized_year = self._normalize_year_for_tag(year)
            if normalized_year:
                with contextlib.suppress(Exception):
                    id3.setall("TYER", [TYER(encoding=3, text=normalized_year)])
                with contextlib.suppress(Exception):
                    id3.setall("TDRC", [TDRC(encoding=3, text=normalized_year)])

            id3.save(path, v2_version=3)
        except Exception:
            return

    def read(self, path: str) -> TagSnapshot:
        if music_tag is None:  # pragma: no cover
            raise ImportError(
                "music-tag is required for tagging. Install music-tag to use Mp3Tagger."
            ) from _music_tag_err

        f = music_tag.load_file(path)
        keys = [
            "tracktitle",
            "artist",
            "album",
            "albumartist",
            "year",
            "date",
            "genre",
            "comment",
            "isrc",
            "tracknumber",
            "discnumber",
            "bpm",
        ]

        tags: dict[str, str] = {}
        for k in keys:
            try:
                if k in f:
                    v = f[k]
                    if isinstance(v, list):
                        tags[k] = ", ".join([str(x) for x in v if x is not None])
                    else:
                        tags[k] = str(v)
            except Exception as e:
                log.error(f"[TAG-READ] {path}: failed reading {k}: {e!r}")

        has_artwork = False
        try:
            has_artwork = "artwork" in f and bool(f["artwork"])
        except Exception:
            has_artwork = False

        return TagSnapshot(tags=tags, has_artwork=has_artwork)

    def write(
        self,
        path: str,
        metadata: Mapping[str, Any],
        *,
        ensure_virtualdj_compat: bool = False,
    ) -> None:
        """Write tags using a flexible metadata mapping.

        Supported keys (case-sensitive):
          - title, artist, album, album_artist, year, genre, comment, isrc,
            track_number, disc_number, bpm

        Extra keys are ignored.
        """

        if music_tag is None:  # pragma: no cover
            raise ImportError(
                "music-tag is required for tagging. Install music-tag to use Mp3Tagger."
            ) from _music_tag_err

        f = music_tag.load_file(path)

        def _get(*keys: str) -> Any | None:
            for k in keys:
                if k in metadata:
                    return metadata.get(k)
            return None

        mapping = {
            "tracktitle": _get("title", "tracktitle"),
            "artist": _get("artist"),
            "album": _get("album"),
            "albumartist": _get("album_artist", "albumartist"),
            "year": _get("year", "date"),
            "genre": _get("genre"),
            "comment": _get("comment"),
            "isrc": _get("isrc"),
            "tracknumber": _get("track_number", "tracknumber"),
            "discnumber": _get("disc_number", "discnumber"),
            "bpm": _get("bpm"),
        }

        for key, val in mapping.items():
            if val is None:
                continue
            try:
                f[key] = str(val)
            except Exception as e:
                log.error(f"[TAG-WRITE] {path}: failed setting {key}={val!r}: {e!r}")

        f.save()
        if ensure_virtualdj_compat:
            self._save_virtualdj_id3_compat(
                path, str(mapping.get("year") or "") or None
            )

    def dump_tags(self, path: str) -> dict[str, str]:
        """Return a stable dict of tags for logging/debug."""
        if music_tag is None:  # pragma: no cover
            return {}
        try:
            f = music_tag.load_file(path)
        except Exception as e:
            log.error(
                f"[TAGS-ERROR] Failed to read tags for {os.path.basename(path)}: {e}"
            )
            return {}

        printed: dict[str, str] = {}

        for k in TAG_FIELDS:
            try:
                v = f[k]
                if isinstance(v, list):
                    printed[k] = ", ".join([str(x) for x in v if x is not None])
                else:
                    printed[k] = "" if v is None else str(v)
            except Exception:
                printed[k] = ""

        # Include extra keys if available
        extra_keys = []
        try:
            extra_keys = [k for k in f if k not in printed and k != "artwork"]
        except Exception:
            extra_keys = []

        for k in sorted(extra_keys):
            try:
                v = f[k]
                if isinstance(v, list):
                    printed[k] = ", ".join([str(x) for x in v if x is not None])
                else:
                    printed[k] = "" if v is None else str(v)
            except Exception:
                continue

        return printed
