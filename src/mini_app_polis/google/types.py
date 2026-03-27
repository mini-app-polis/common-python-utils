from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str | None = None
    modified_time: str | None = None
