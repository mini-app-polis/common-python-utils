from __future__ import annotations

import time
from typing import Any

import musicbrainzngs

from ..identifier import TrackId, TrackMetadata


class MusicBrainzRecordingProvider:
    """Fetch metadata for a MusicBrainz recording MBID.

    Returns a plain dict so callers can pass it to other modules without shared types.
    """

    def __init__(
        self,
        app_name: str = "identify-audio",
        app_version: str = "0.1.0",
        contact: str = "",
        throttle_s: float = 1.0,
        retries: int = 3,
        retry_sleep_s: float = 1.0,
    ) -> None:
        self.throttle_s = float(throttle_s)
        self.retries = int(retries)
        self.retry_sleep_s = float(retry_sleep_s)
        self._last_call_ts = 0.0

        musicbrainzngs.set_useragent(app_name, app_version, contact)

    def _throttle(self) -> None:
        delta = time.time() - self._last_call_ts
        if delta < self.throttle_s:
            time.sleep(self.throttle_s - delta)
        self._last_call_ts = time.time()

    def _best_genre(self, tags: list[dict[str, Any]] | None) -> str | None:
        if not tags:
            return None
        try:
            sorted_tags = sorted(
                tags, key=lambda t: int(t.get("count", 0) or 0), reverse=True
            )
        except Exception:
            sorted_tags = tags
        for t in sorted_tags:
            name = t.get("name")
            if name:
                return str(name)
        return None

    def fetch(self, track_id: TrackId) -> TrackMetadata:
        if track_id.provider != "musicbrainz":
            raise ValueError(
                "MusicBrainzRecordingProvider only supports provider='musicbrainz', "
                f"got {track_id.provider!r}"
            )

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self._throttle()
                rec = musicbrainzngs.get_recording_by_id(
                    track_id.id,
                    includes=["artists", "releases", "isrcs", "tags"],
                )
                r: dict[str, Any] = (
                    rec.get("recording", {}) if isinstance(rec, dict) else {}
                )

                title = r.get("title")
                artist = None
                album = None
                isrc = None
                year = None
                genre = None

                # primary artist credit
                try:
                    ac = r.get("artist-credit") or []
                    if ac and isinstance(ac, list):
                        a0 = ac[0]
                        if isinstance(a0, dict):
                            artist = (a0.get("artist") or {}).get("name") or a0.get(
                                "name"
                            )
                except Exception:
                    artist = None

                # release / album + year
                try:
                    releases = r.get("release-list") or []
                    if releases:
                        rel0 = releases[0]
                        if isinstance(rel0, dict):
                            album = rel0.get("title")
                            date = rel0.get("date")
                            if date and isinstance(date, str) and len(date) >= 4:
                                year = date[:4]
                except Exception:
                    pass

                # isrc
                try:
                    isrcs = r.get("isrc-list") or []
                    if isrcs:
                        isrc = str(isrcs[0])
                except Exception:
                    isrc = None

                # tags -> genre
                try:
                    tags = r.get("tag-list") or []
                    genre = self._best_genre(tags)
                except Exception:
                    genre = None

                return {
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "isrc": isrc,
                    "genre": genre,
                    "raw": {"musicbrainz_recording": r},
                    "_provider": "musicbrainz",
                    "_mbid": track_id.id,
                }

            except Exception as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(self.retry_sleep_s * attempt)
                else:
                    raise RuntimeError(
                        f"MusicBrainz fetch failed for {track_id.id}: {last_err!r}"
                    ) from last_err

        raise RuntimeError(f"MusicBrainz fetch failed for {track_id.id}: {last_err!r}")
