from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TagSnapshot:
    """Hold normalized tag values and artwork presence for one file."""

    tags: dict[str, str] = field(default_factory=dict)
    has_artwork: bool = False
