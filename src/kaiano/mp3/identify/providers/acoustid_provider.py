from __future__ import annotations

import binascii
import os
import subprocess
import time
from collections.abc import Iterable

# Optional dep (installed by caller environment)
import acoustid

try:
    import kaiano.logger as log  # type: ignore
except Exception:  # pragma: no cover
    import logging

    log = logging.getLogger(__name__)

from ..identifier import TrackId


class AcoustIdIdentifier:
    """Identify audio using Chromaprint fingerprints + AcoustID lookup.

    Returns MusicBrainz recording MBIDs as TrackId(provider="musicbrainz").

    Note: This class is self-contained (no dependency on tag/name modules).
    """

    def __init__(
        self,
        api_key: str,
        min_confidence: float = 0.90,
        max_candidates: int = 5,
        retries: int = 3,
        retry_sleep_s: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.min_confidence = float(min_confidence)
        self.max_candidates = int(max_candidates)
        self.retries = int(retries)
        self.retry_sleep_s = float(retry_sleep_s)

    def identify(self, path: str) -> Iterable[TrackId]:
        basename = os.path.basename(path)

        for attempt in range(1, self.retries + 1):
            try:
                # acoustid.match may return an iterator/generator depending on version
                results = list(acoustid.match(self.api_key, path))
                log.info(
                    f"[ACOUSTID-RAW] {basename}: match() returned {len(results)} rows"
                )

                candidates: list[TrackId] = []
                for score, recording_id, title, artist in results:
                    try:
                        score_f = float(score)
                    except Exception:
                        score_f = 0.0

                    log.info(
                        f"[ACOUSTID-RAW] score={score_f:.3f} mbid={recording_id!r} artist={artist!r} title={title!r}"
                    )

                    if not recording_id:
                        continue
                    if score_f < self.min_confidence:
                        continue

                    candidates.append(
                        TrackId(
                            provider="musicbrainz",
                            id=str(recording_id),
                            confidence=score_f,
                        )
                    )

                candidates.sort(key=lambda c: c.confidence, reverse=True)
                return candidates[: self.max_candidates]

            except Exception as e:
                # Debug: file header + size
                try:
                    with open(path, "rb") as fh:
                        head = fh.read(32)
                    head32_hex = binascii.hexlify(head).decode("ascii", errors="ignore")
                    size_bytes = os.path.getsize(path)
                    log.error(
                        f"[ACOUSTID-ERROR] {basename}: {e!r} size_bytes={size_bytes} head32_hex={head32_hex}"
                    )
                except Exception as _dbg_e:
                    log.error(
                        f"[ACOUSTID-ERROR] {basename}: {e!r} (dbg failed: {_dbg_e!r})"
                    )

                # Fallback: fpcalc -> acoustid.lookup
                try:
                    p = subprocess.run(
                        ["fpcalc", "-json", path],
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    if getattr(p, "returncode", 1) == 0 and p.stdout:
                        data = p.stdout
                        duration = None
                        fingerprint = None
                        try:
                            import json as _json

                            json_text = data
                            if "{" in json_text and "}" in json_text:
                                json_text = json_text[
                                    json_text.find("{") : json_text.rfind("}") + 1
                                ]

                            parsed = _json.loads(json_text)
                            duration = parsed.get("duration")
                            fingerprint = parsed.get("fingerprint")
                        except Exception as _json_e:
                            log.error(
                                f"[ACOUSTID-FALLBACK-PARSE-ERROR] {basename}: {_json_e!r} sample={data[:120]!r}"
                            )

                        if duration and fingerprint:
                            log.info(
                                f"[ACOUSTID-FALLBACK] {basename}: using fpcalc fingerprint for lookup (duration={duration})"
                            )
                            try:
                                lookup = acoustid.lookup(
                                    self.api_key,
                                    fingerprint,
                                    duration,
                                    meta="recordings+releasegroups+compress",
                                )
                                results2 = (
                                    lookup.get("results", [])
                                    if isinstance(lookup, dict)
                                    else []
                                )
                                candidates2: list[TrackId] = []

                                for r in results2:
                                    score = float(r.get("score", 0.0) or 0.0)
                                    if score < self.min_confidence:
                                        continue
                                    recs = r.get("recordings") or []
                                    for rec in recs:
                                        rid = rec.get("id")
                                        if rid:
                                            candidates2.append(
                                                TrackId(
                                                    provider="musicbrainz",
                                                    id=str(rid),
                                                    confidence=score,
                                                )
                                            )

                                candidates2.sort(
                                    key=lambda c: c.confidence, reverse=True
                                )
                                return candidates2[: self.max_candidates]
                            except Exception as _lookup_e:
                                log.error(
                                    f"[ACOUSTID-FALLBACK-LOOKUP-ERROR] {basename}: {_lookup_e!r}"
                                )

                except Exception as _fpcalc_e:
                    log.error(f"[ACOUSTID-FALLBACK-ERROR] {basename}: {_fpcalc_e!r}")

                log.error(
                    f"[ACOUSTID-ERROR] {basename} attempt {attempt}/{self.retries}: {e!r}"
                )
                if attempt < self.retries:
                    time.sleep(self.retry_sleep_s * attempt)
                else:
                    return []

        return []
