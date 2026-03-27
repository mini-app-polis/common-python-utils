from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# NOTE: Optional dependencies are imported lazily in from_env().


@dataclass(frozen=True)
class TrackId:
    provider: str
    id: str
    confidence: float = 0.0


TrackMetadata = dict[str, Any]


@dataclass(frozen=True)
class IdentificationPolicy:
    """Policy knobs for identification."""

    min_confidence: float = 0.90
    max_candidates: int = 5
    fetch_metadata_min_confidence: float = 0.90


@dataclass(frozen=True)
class IdentificationResult:
    path: str
    candidates: list[TrackId]
    chosen: TrackId | None
    metadata: TrackMetadata | None
    snapshot: dict[str, str] = field(default_factory=dict)


class Mp3Identifier:
    """Identify MP3 files (AcoustID) and optionally fetch metadata (MusicBrainz).

    This package intentionally does NOT depend on tag/ or name/. It can *optionally*
    read existing tags for context via an internal snapshot reader.

    Data contract:
    - candidates/chosen are TrackId
    - metadata is a plain dict (TrackMetadata)
    """

    def __init__(
        self,
        *,
        acoustid_identifier,
        musicbrainz_provider,
        policy: IdentificationPolicy,
        snapshot_reader=None,
    ) -> None:
        self._acoustid = acoustid_identifier
        self._mb = musicbrainz_provider
        self._policy = policy
        self._snapshot_reader = snapshot_reader

    @classmethod
    def from_env(
        cls,
        *,
        acoustid_api_key: str,
        policy: IdentificationPolicy | None = None,
        app_name: str = "identify-audio",
        app_version: str = "0.1.0",
        contact: str = "",
        throttle_s: float = 1.0,
        enable_tag_snapshot: bool = True,
    ) -> Mp3Identifier:
        """Create an identifier with optional deps loaded lazily."""

        policy = policy or IdentificationPolicy()

        from .providers.acoustid_provider import AcoustIdIdentifier
        from .providers.musicbrainz_provider import MusicBrainzRecordingProvider

        acoustid_identifier = AcoustIdIdentifier(
            api_key=acoustid_api_key,
            min_confidence=policy.min_confidence,
            max_candidates=policy.max_candidates,
        )

        musicbrainz_provider = MusicBrainzRecordingProvider(
            app_name=app_name,
            app_version=app_version,
            contact=contact,
            throttle_s=throttle_s,
        )

        snapshot_reader = None
        if enable_tag_snapshot:
            from .io.tag_snapshot import MusicTagSnapshotReader

            snapshot_reader = MusicTagSnapshotReader()

        return cls(
            acoustid_identifier=acoustid_identifier,
            musicbrainz_provider=musicbrainz_provider,
            policy=policy,
            snapshot_reader=snapshot_reader,
        )

    def identify(
        self, path: str, *, fetch_metadata: bool = True
    ) -> IdentificationResult:
        snapshot: dict[str, str] = {}
        if self._snapshot_reader is not None:
            try:
                snapshot = self._snapshot_reader.read(path)
            except Exception:
                snapshot = {}

        candidates = list(self._acoustid.identify(path))

        chosen: TrackId | None = None
        if candidates:
            chosen = max(candidates, key=lambda c: float(c.confidence or 0.0))

        metadata: TrackMetadata | None = None
        if fetch_metadata and chosen is not None:
            conf = float(chosen.confidence or 0.0)
            if conf >= float(self._policy.fetch_metadata_min_confidence):
                metadata = self._mb.fetch(chosen)

        return IdentificationResult(
            path=path,
            candidates=candidates,
            chosen=chosen,
            metadata=metadata,
            snapshot=snapshot,
        )
