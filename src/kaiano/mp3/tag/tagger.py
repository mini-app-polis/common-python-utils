from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .io.music_tag_io import MusicTagIO
from .models import TagSnapshot


class Mp3Tagger:
    """Read/write tags on local MP3 files via `music-tag`.

    This module is fully self-contained (no dependency on identify/name).

    Contract:
    - read() returns TagSnapshot (tags are strings)
    - write() accepts a metadata mapping (dict-like). Keys are flexible but common ones are:
      title, artist, album, album_artist, year, genre, comment, isrc, track_number,
      disc_number, bpm
    """

    def __init__(self, io: MusicTagIO | None = None):
        self._io = io or MusicTagIO()

    def read(self, path: str) -> TagSnapshot:
        return self._io.read(path)

    def write(
        self,
        path: str,
        metadata: Mapping[str, Any],
        *,
        ensure_virtualdj_compat: bool = False,
    ) -> None:
        self._io.write(path, metadata, ensure_virtualdj_compat=ensure_virtualdj_compat)

    def dump(self, path: str) -> dict[str, str]:
        return self._io.dump_tags(path)

    @staticmethod
    def sanitize_string(v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @staticmethod
    def build_routine_tag_title(
        *, leader_first: str, leader_last: str, follower_first: str, follower_last: str
    ) -> str:
        """Build the routine title tag.

        Intended behavior:
        - Post-sanitization empty strings are treated as missing values
        - If both leader and follower are present: "Leader First Leader Last & Follower First Follower Last"
        - If only one side is present: return that name only (no dangling '&')
        - If all inputs are empty: return ""
        """

        leader_parts = [
            Mp3Tagger.sanitize_string(leader_first),
            Mp3Tagger.sanitize_string(leader_last),
        ]
        leader_parts = [p for p in leader_parts if p]
        leader = " ".join(leader_parts)

        follower_parts = [
            Mp3Tagger.sanitize_string(follower_first),
            Mp3Tagger.sanitize_string(follower_last),
        ]
        follower_parts = [p for p in follower_parts if p]
        follower = " ".join(follower_parts)

        if leader and follower:
            return f"{leader} & {follower}"
        return leader or follower

    @staticmethod
    def build_routine_tag_artist(
        *,
        version: str,
        division: str,
        season_year: str,
        routine_name: str,
        personal_descriptor: str,
    ) -> str:
        base = f"v{Mp3Tagger.sanitize_string(version)} | {Mp3Tagger.sanitize_string(division)} {Mp3Tagger.sanitize_string(season_year)}".strip()
        parts = [base]
        rn = Mp3Tagger.sanitize_string(routine_name)
        pd = Mp3Tagger.sanitize_string(personal_descriptor)
        if rn:
            parts.append(rn)
        if pd:
            parts.append(pd)
        return " | ".join([p for p in parts if p])
