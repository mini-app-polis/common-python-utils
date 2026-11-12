from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Mapping
from typing import Any

from ..models import TagSnapshot

music_tag: Any | None
_music_tag_err: Exception | None
try:
    import music_tag as _music_tag
except Exception as e:  # pragma: no cover
    music_tag = None
    _music_tag_err = e
else:
    music_tag = _music_tag
    _music_tag_err = None

ID3: Any | None
TDRC: Any | None
TYER: Any | None
ID3NoHeaderError: Any | None
_mutagen_err: Exception | None
try:
    from mutagen.id3 import ID3, TDRC, TYER, ID3NoHeaderError
except Exception as e:  # pragma: no cover
    ID3 = TDRC = TYER = ID3NoHeaderError = None
    _mutagen_err = e
else:
    _mutagen_err = None

# Any rather than Any | None: each is checked through _mutagen_file first.
_mutagen_file: Any
MP4FreeForm: Any
ID3Tags: Any
MP4Tags: Any
TBPM: Any
TPUB: Any
try:
    from mutagen import File as _mutagen_file
    from mutagen.id3 import ID3 as ID3Tags
    from mutagen.id3 import TBPM, TPUB
    from mutagen.mp4 import MP4FreeForm, MP4Tags
except Exception:  # pragma: no cover
    _mutagen_file = MP4FreeForm = ID3Tags = MP4Tags = TBPM = TPUB = None

#: The fields music_tag does not read or write in any format, kept per
#: format's own key instead: ID3 (MP3, WAV, AIFF) TBPM/TPUB, Vorbis comments
#: (FLAC, Ogg Vorbis, Ogg Opus) BPM/LABEL, MP4 tmpo and a LABEL freeform atom.
EXTRA_FIELDS = ("bpm", "label")
_MP4_LABEL = "----:com.apple.iTunes:LABEL"


def _bpm_text(value: Any) -> str:
    """A BPM as text: a whole number without its ".0"."""
    s = str(value).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else s


def read_extras(path: str) -> dict[str, str]:
    """BPM and label from the file's own tag block, by its format's keys."""
    if _mutagen_file is None:  # pragma: no cover
        return {}
    m = _mutagen_file(path)
    tags = getattr(m, "tags", None)
    if tags is None:
        return {}
    out: dict[str, str] = {}
    if isinstance(tags, ID3Tags):
        for key, frame in (("bpm", "TBPM"), ("label", "TPUB")):
            if frame in tags and str(tags[frame]).strip():
                out[key] = str(tags[frame]).strip()
    elif isinstance(tags, MP4Tags):
        if tags.get("tmpo"):
            out["bpm"] = str(tags["tmpo"][0])
        if tags.get(_MP4_LABEL):
            out["label"] = bytes(tags[_MP4_LABEL][0]).decode("utf-8").strip()
    else:
        for key, name in (("bpm", "BPM"), ("label", "LABEL")):
            values = tags.get(name) or tags.get(name.lower())
            if values and str(values[0]).strip():
                out[key] = str(values[0]).strip()
    if "bpm" in out:
        out["bpm"] = _bpm_text(out["bpm"])
    return out


def write_extras(path: str, values: Mapping[str, Any]) -> None:
    """Write BPM and label, where given, by the file's format's own keys."""
    wanted = {k: v for k, v in values.items() if k in EXTRA_FIELDS and v is not None}
    if not wanted or _mutagen_file is None:
        return
    m = _mutagen_file(path)
    if m is None:
        return
    if m.tags is None:
        m.add_tags()
    tags = m.tags
    if isinstance(tags, ID3Tags):
        if "bpm" in wanted:
            tags.setall("TBPM", [TBPM(encoding=3, text=[_bpm_text(wanted["bpm"])])])
        if "label" in wanted:
            tags.setall("TPUB", [TPUB(encoding=3, text=[str(wanted["label"])])])
    elif isinstance(tags, MP4Tags):
        if "bpm" in wanted:
            tags["tmpo"] = [round(float(wanted["bpm"]))]
        if "label" in wanted:
            tags[_MP4_LABEL] = [MP4FreeForm(str(wanted["label"]).encode("utf-8"))]
    else:
        if "bpm" in wanted:
            tags["BPM"] = [_bpm_text(wanted["bpm"])]
        if "label" in wanted:
            tags["LABEL"] = [str(wanted["label"])]
    m.save()


try:
    import mini_app_polis.logger as logger_mod
except Exception:  # pragma: no cover
    import logging

    log = logging.getLogger(__name__)
else:
    log = logger_mod.get_logger()

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


#: A year at the start of a tag value: "2000", "2000-11-30", "2000-11-30T08:00:00Z".
_LEADING_YEAR = re.compile(r"^\s*(\d{4})(?!\d)")


def _year_from_raw(f: Any, key: str) -> str:
    """The year of a ``year`` tag music_tag can't parse, or "".

    music_tag reads a year only as a bare number or a plain date, so an
    ISO timestamp such as ``2000-11-30T08:00:00Z`` (common in M4A files)
    raises. The raw value is read untyped and its leading four digits kept.
    """
    if key != "year":
        return ""
    try:
        raw = f.get(key, typeless=True)
    except Exception:
        return ""
    values = getattr(raw, "values", None) or [raw]
    for value in values:
        if value is None:
            continue
        found = _LEADING_YEAR.match(str(value))
        if found:
            return found.group(1)
    return ""


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
            if (
                ID3 is None or ID3NoHeaderError is None or TYER is None or TDRC is None
            ):  # pragma: no cover
                return
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext != "mp3":
                return

            try:
                id3 = ID3(path)
            except Exception:
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
        """Read local audio tags into a normalized TagSnapshot."""
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
            "genre",
            "comment",
            "isrc",
            "tracknumber",
            "discnumber",
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
                year = _year_from_raw(f, k)
                if year:
                    tags[k] = year
                    continue
                # The path goes in extra, not the message, so repeats of one
                # failure share a message (and one Sentry issue).
                log.error(
                    "[TAG-READ] failed reading %s: %r", k, e, extra={"path": path}
                )

        try:
            tags.update(read_extras(path))
        except Exception as e:
            log.error(
                "[TAG-READ] failed reading bpm/label: %r", e, extra={"path": path}
            )

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
            track_number, disc_number, bpm, label (bpm and label by each
            format's own key: see EXTRA_FIELDS)

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
        }

        for key, val in mapping.items():
            if val is None:
                continue
            try:
                f[key] = str(val)
            except Exception as e:
                log.error(f"[TAG-WRITE] {path}: failed setting {key}={val!r}: {e!r}")

        f.save()
        try:
            write_extras(path, {"bpm": _get("bpm"), "label": _get("label")})
        except Exception as e:
            log.error(f"[TAG-WRITE] {path}: failed setting bpm/label: {e!r}")
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
