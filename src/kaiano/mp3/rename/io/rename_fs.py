from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    import kaiano.helpers as helpers  # type: ignore
except Exception:  # pragma: no cover
    helpers = None


def _safe_filename_component_fallback(value: str | None) -> str:
    if value is None:
        return ""
    # Keep it simple: strip, replace spaces, and drop path separators.
    s = str(value).strip().replace(" ", "_")
    s = s.replace(os.sep, "_")
    s = s.replace("/", "_").replace("\\", "_")
    bad = '<>:"|?*'
    for ch in bad:
        s = s.replace(ch, "")
    return s


def safe_str(v: Any) -> str:
    """Best-effort stringify without turning missing values into the literal 'None'."""
    if v is None:
        return ""
    try:
        s = str(v)
    except Exception:
        return ""
    # Some tag wrappers stringify missing values as "None"
    if s.strip().lower() == "none":
        return ""
    return s


def safe_filename_component(v: Any) -> str:
    """
    Normalize a value for safe, deterministic filenames.

    Rules:
    - Convert to string
    - Strip accents / diacritics
    - Lowercase
    - Remove all whitespace
    - Remove all non-alphanumeric characters (except underscore)
    - Collapse multiple underscores
    """
    s = safe_str(v)

    if not s:
        return ""

    # Normalize unicode (e.g. Beyoncé -> Beyonce)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))

    s = s.lower()

    # Remove whitespace entirely
    s = re.sub(r"\s+", "", s)

    # Replace any remaining invalid chars with underscore
    s = re.sub(r"[^a-z0-9_]", "_", s)

    # Collapse multiple underscores
    s = re.sub(r"_+", "_", s)

    return s.strip("_")


def _safe_component(value: str | None) -> str:
    if helpers is not None and hasattr(helpers, "safe_filename_component"):
        return helpers.safe_filename_component(value)  # type: ignore[attr-defined]
    return _safe_filename_component_fallback(value)


@dataclass
class RenameProposal:
    src_path: str
    dest_path: str
    dest_name: str


class RenameFacade:
    """Local-only filename proposal (no rename on disk).

    This module does not depend on identify/tag types.

    Provides a single-call build_filename method that computes a destination filename (no filesystem side effects).
    """

    def build_filename(
        self,
        path: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        title: str | None = None,
        artist: str | None = None,
        template: str = "{title}_{artist}",
        fallback_to_original: bool = True,
    ) -> str:
        """Compute the destination filename for a path.

        Intent (confirmed): callers provide title/artist (or metadata containing them)
        and receive a single filename string; there is no propose/apply flow and no
        filesystem side effects.

        Behavior:
        - If `metadata` is provided, title/artist fall back to metadata values.
        - If both sanitized title and artist are present, build the filename from the template.
        - Otherwise, if `fallback_to_original` is True, keep the original filename.
        """

        if metadata is not None:
            # Mapping-like; prefer explicit args and fall back to metadata.
            title = title or (
                metadata.get("title") if hasattr(metadata, "get") else None
            )  # type: ignore[arg-type]
            artist = artist or (
                metadata.get("artist") if hasattr(metadata, "get") else None
            )  # type: ignore[arg-type]

        original_name = os.path.basename(path)
        _, ext = os.path.splitext(original_name)

        title_part = _safe_component(title)
        artist_part = _safe_component(artist)

        if title_part and artist_part:
            return template.format(title=title_part, artist=artist_part) + ext

        return original_name if fallback_to_original else original_name

    def rename(
        self,
        path: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        title: str | None = None,
        artist: str | None = None,
        template: str = "{title}_{artist}",
        fallback_to_original: bool = True,
    ) -> str:
        """Legacy alias; prefer `build_filename(...)`.

        Kept to avoid breaking older callers; returns the destination filename string.
        """
        return self.build_filename(
            path,
            metadata=metadata,
            title=title,
            artist=artist,
            template=template,
            fallback_to_original=fallback_to_original,
        )
