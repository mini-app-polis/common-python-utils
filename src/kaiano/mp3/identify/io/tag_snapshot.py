from __future__ import annotations

try:
    import music_tag  # type: ignore
except Exception as e:  # pragma: no cover
    music_tag = None
    _import_err = e
else:
    _import_err = None


class MusicTagSnapshotReader:
    """Lightweight, read-only tag snapshot.

    This lives inside the identify module so identify has *no dependency* on tag/.
    """

    _KEYS = [
        "tracktitle",
        "artist",
        "album",
        "albumartist",
        "year",
        "date",
        "genre",
        "comment",
        "isrc",
        "tracknumber",
        "discnumber",
        "bpm",
    ]

    def read(self, path: str) -> dict[str, str]:
        if music_tag is None:  # pragma: no cover
            raise ImportError(
                "music-tag is required for tag snapshots. Install music-tag to enable this."
            ) from _import_err

        f = music_tag.load_file(path)
        out: dict[str, str] = {}
        for k in self._KEYS:
            try:
                if k in f:
                    v = f[k]
                    if isinstance(v, list):
                        out[k] = ", ".join([str(x) for x in v if x is not None])
                    else:
                        out[k] = "" if v is None else str(v)
            except Exception:
                continue
        return out
