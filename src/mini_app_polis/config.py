from __future__ import annotations

import os

# Google / VDJ
VDJ_HISTORY_FOLDER_ID = os.getenv("VDJ_HISTORY_FOLDER_ID")
TIMEZONE = os.getenv("TIMEZONE", "UTC")

# Spotify — read lazily via properties so env vars set after import are picked up


def __getattr__(name: str):
    _spotify_vars = {
        "SPOTIPY_CLIENT_ID",
        "SPOTIPY_CLIENT_SECRET",
        "SPOTIPY_REDIRECT_URI",
        "SPOTIPY_REFRESH_TOKEN",
        "SPOTIFY_PLAYLIST_ID",
    }
    if name in _spotify_vars:
        return os.getenv(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
