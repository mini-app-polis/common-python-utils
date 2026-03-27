from __future__ import annotations

import re
import string

_FEAT_RE = re.compile(r"\s*(feat\.|ft\.|featuring)\s+.*$", re.IGNORECASE)
_PAREN_SUFFIX_RE = re.compile(
    r"\s*\((radio edit|clean|acoustic|remix|original mix|clean version)\)\s*$",
    re.IGNORECASE,
)


def _normalize_base(value: str) -> str:
    value = value.lower()
    value = _FEAT_RE.sub("", value)
    value = _PAREN_SUFFIX_RE.sub("", value)
    value = value.strip()
    value = value.strip(string.punctuation + " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_for_matching(title: str, artist: str) -> tuple[str, str]:
    """
    Normalize raw title + artist strings for fuzzy matching.

    Strips feat. credits, common parenthetical suffixes (radio edit,
    clean, acoustic, etc.), excess whitespace, and punctuation.
    Returns (normalized_title, normalized_artist).
    """
    return _normalize_base(title), _normalize_base(artist)
