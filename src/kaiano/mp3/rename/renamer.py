from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .io.rename_fs import RenameFacade


class Mp3Renamer:
    """Compute filenames for local MP3 files based on metadata.

    This class only computes destination filenames and does not perform any filesystem operations.

    Metadata contract:
    - accepts a mapping (dict-like) with optional keys: title, artist
    """

    def __init__(self, facade: RenameFacade | None = None):
        self._rename = facade or RenameFacade()

    def rename(
        self,
        path: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        title: str | None = None,
        artist: str | None = None,
        template: str = "{title}_{artist}",
        fallback_to_original: bool = True,
    ) -> str:
        if metadata is not None:
            title = title or metadata.get("title")  # type: ignore[arg-type]
            artist = artist or metadata.get("artist")  # type: ignore[arg-type]

        # Delegate filename construction to the facade, but do NOT rename on disk
        name = self._rename.build_filename(
            path,
            title=title,
            artist=artist,
            template=template,
            fallback_to_original=fallback_to_original,
        )

        return name

    @staticmethod
    def sanitize_string(v: Any) -> str:
        """Return a filename-safe token.

        Intent (confirmed):
        - None -> "" (treated as not provided)
        - Collapse all internal whitespace to a single underscore
        - Remove all non-alphanumeric characters except underscores
        - Strip leading/trailing underscores

        This is intentionally more aggressive than tag sanitization:
        filenames must be stable and filesystem-safe.
        """
        if v is None:
            return ""

        s = str(v).strip()
        if not s:
            return ""

        # Collapse all whitespace to single underscores
        s = re.sub(r"\s+", "_", s)

        # Remove all non-alphanumeric / underscore characters
        s = re.sub(r"[^A-Za-z0-9_]", "", s)

        # Avoid leading/trailing separators
        return s.strip("_")

    # build_routine_filename relies on sanitize_string to ensure filesystem safety
    @staticmethod
    def build_routine_filename(
        leader, follower, division, routine, descriptor, season_year
    ) -> str:
        """Return base_without_version_or_ext. Base includes season year and optional fields."""

        prefix = "_".join(
            [
                (Mp3Renamer.sanitize_string(leader)),
                (Mp3Renamer.sanitize_string(follower)),
                (Mp3Renamer.sanitize_string(division)),
            ]
        )

        tail_parts: list[str] = [Mp3Renamer.sanitize_string(season_year)]
        if routine:
            tail_parts.append(Mp3Renamer.sanitize_string(routine))
        if descriptor:
            tail_parts.append(Mp3Renamer.sanitize_string(descriptor))

        return f"{prefix}_{'_'.join(tail_parts)}"
