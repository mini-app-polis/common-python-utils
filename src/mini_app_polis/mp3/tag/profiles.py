"""Rule profiles: which fields a library's files carry, and who wins.

A profile names the fields written to a file, whose display rules write
them out, and whether the file or the catalog wins when the two differ
(deejay-cog ADR-0010). An app chooses which profile applies to what; a
profile holds no folder ids.

- ``main``: the catalog wins. The owned fields are written to the file as
  the catalog's display rules show them (deejay-cog ADR-0008).
- ``fx``: the file wins. The file keeps its own values, and a value the
  catalog lacks or holds differently goes up to the catalog instead.

:func:`apply_profile` compares one file's tags with its catalog values under
a profile, through the multi-format :class:`AudioTagger`, and returns what
changes: the values written to the file, and the values to send up. It
never empties a tag or an entry's value, and with ``dry_run`` it writes
nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .audio_tagger import AudioTagger
from .models import AudioFormat, UnsupportedFile


class Winner(StrEnum):
    """Which side's value stands when a file and its catalog entry differ."""

    CATALOG = "catalog"
    FILE = "file"


#: The fields a profile can own, as the tagger's metadata keys.
PROFILE_FIELDS: tuple[str, ...] = ("title", "artist", "year", "genre", "bpm", "label")

#: Where each field is found in a TagSnapshot's tags.
_READ_KEY = {
    "title": "tracktitle",
    "artist": "artist",
    "year": "year",
    "genre": "genre",
    "bpm": "bpm",
    "label": "label",
}


@dataclass(frozen=True)
class RuleProfile:
    """A named set of tagging rules."""

    name: str
    #: The fields the profile owns, from PROFILE_FIELDS.
    fields: tuple[str, ...]
    winner: Winner
    #: Whose display rules write the values out: "catalog" (as the catalog
    #: shows its entry) or "file" (the file's own spelling stands).
    display: str

    def __post_init__(self) -> None:
        unknown = [f for f in self.fields if f not in PROFILE_FIELDS]
        if unknown:
            raise ValueError(f"profile {self.name!r} names unknown fields {unknown}")


MAIN = RuleProfile(
    name="main", fields=PROFILE_FIELDS, winner=Winner.CATALOG, display="catalog"
)
FX = RuleProfile(name="fx", fields=PROFILE_FIELDS, winner=Winner.FILE, display="file")

#: Every profile, by name.
PROFILES: dict[str, RuleProfile] = {p.name: p for p in (MAIN, FX)}


def get_profile(name: str) -> RuleProfile:
    """The profile called ``name``; ValueError naming the known ones otherwise."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown rule profile {name!r}; known: {sorted(PROFILES)}"
        ) from None


#: The roles an FX file's comment can carry (deejay-cog ADR-0010).
FX_ROLES: tuple[str, ...] = ("Walk On", "Bow", "MC Intro", "Rotation")

_FX_PREFIX = "<FX>"


def _following_part(comment: str | None) -> tuple[str, str] | None:
    """For a comment starting with ``<FX>`` (leading whitespace ignored): the
    text after it and the name inside the next ``<...>`` part, or "" when no
    such part follows. None when the comment does not start with ``<FX>``."""
    text = (comment or "").lstrip()
    if not text.startswith(_FX_PREFIX):
        return None
    rest = text[len(_FX_PREFIX) :]
    if rest.startswith("<") and (end := rest.find(">")) > 0:
        return rest, rest[1:end]
    return rest, ""


def role_conflict(comment: str | None, role: str) -> str | None:
    """The other known role a comment already carries right after ``<FX>``,
    or None. :func:`role_comment` leaves such a comment unchanged; a caller
    reports it."""
    found = _following_part(comment)
    if found is None:
        return None
    name = found[1]
    return name if name in FX_ROLES and name != role else None


def role_comment(comment: str | None, role: str) -> str | None:
    """The comment with ``<FX><role>`` at its start, or None when nothing
    changes (deejay-cog ADR-0010).

    Unchanged: it already starts with ``<FX><role>``, or ``<FX>`` is followed
    by another known role (tell that apart with :func:`role_conflict`).
    ``<FX>`` followed by anything else gets ``<role>`` inserted after it;
    any other comment gets ``<FX><role>`` in front, with a space between
    unless it starts with "<". Leading whitespace is ignored; the rest of
    the comment is kept as is.
    """
    found = _following_part(comment)
    part = f"<{role}>"
    if found is not None:
        rest, name = found
        if name in FX_ROLES:
            return None
        return _FX_PREFIX + part + rest
    text = (comment or "").lstrip()
    if not text:
        return _FX_PREFIX + part
    sep = "" if text.startswith("<") else " "
    return _FX_PREFIX + part + sep + text


@dataclass(frozen=True)
class FieldChange:
    """One field where the file and the catalog differ."""

    field: str
    file_value: str | None
    catalog_value: str | None


@dataclass(frozen=True)
class ProfileResult:
    """What applying a profile to one file changes, or would."""

    path: str
    profile: str
    format: AudioFormat | None = None
    #: Set when the tagger cannot tag the file; nothing else is then done.
    unsupported: UnsupportedFile | None = None
    #: Catalog values written to the file (main), or that would be in a dry run.
    to_file: tuple[FieldChange, ...] = ()
    #: File values to send up to the catalog (fx).
    to_catalog: tuple[FieldChange, ...] = ()
    #: Whether the file was written.
    written: bool = False


def _norm(field: str, value: Any) -> str | None:
    """A value as compared: blank is None; a year is its four digits; a BPM
    is a number, so 124, "124" and 124.0 are the same."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if field == "year":
        return s[:4] if len(s) >= 4 and s[:4].isdigit() else s
    if field == "bpm":
        try:
            f = float(s)
        except ValueError:
            return s
        return str(int(f)) if f.is_integer() else str(f)
    return s


def apply_profile(
    path: str,
    profile: RuleProfile,
    catalog: Mapping[str, Any],
    *,
    dry_run: bool = False,
    tagger: AudioTagger | None = None,
) -> ProfileResult:
    """Compare the file at ``path`` with its ``catalog`` values (keyed by
    PROFILE_FIELDS) under ``profile``, and write the catalog's values to the
    file when the catalog wins and this is not a dry run.

    Where the catalog wins, a value it lacks never empties the file's tag;
    where the file wins, an empty file field never empties the entry's
    value. A file the tagger cannot tag comes back with ``unsupported`` set
    and is not touched.
    """
    t = tagger or AudioTagger()
    snap = t.read(path)
    if isinstance(snap, UnsupportedFile):
        return ProfileResult(path=path, profile=profile.name, unsupported=snap)
    detected = t.detect(path)
    fmt = detected if isinstance(detected, AudioFormat) else None

    to_file: list[FieldChange] = []
    to_catalog: list[FieldChange] = []
    for field in profile.fields:
        file_value = _norm(field, snap.tags.get(_READ_KEY[field]))
        catalog_value = _norm(field, catalog.get(field))
        if file_value == catalog_value:
            continue
        change = FieldChange(field, file_value, catalog_value)
        if profile.winner is Winner.CATALOG:
            if catalog_value is not None:
                to_file.append(change)
        elif file_value is not None:
            to_catalog.append(change)

    written = False
    if to_file and not dry_run:
        t.write(path, {c.field: c.catalog_value for c in to_file})
        written = True
    return ProfileResult(
        path=path,
        profile=profile.name,
        format=fmt,
        to_file=tuple(to_file),
        to_catalog=tuple(to_catalog),
        written=written,
    )
