import importlib
import sys
import types

import pytest


def _install(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


def _install_stubs(monkeypatch, *, uris: list[str], configured_playlist="pl-main"):
    """Install minimal spotipy/requests stubs over a stateful fake playlist."""
    cfg = sys.modules.get("mini_app_polis.config")
    if cfg is None:
        cfg = types.ModuleType("mini_app_polis.config")
        _install("mini_app_polis.config", cfg)
    cfg.SPOTIPY_CLIENT_ID = "cid"
    cfg.SPOTIPY_CLIENT_SECRET = "secret"
    cfg.SPOTIPY_REFRESH_TOKEN = "refresh"
    cfg.SPOTIPY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
    cfg.SPOTIFY_PLAYLIST_ID = configured_playlist

    if "requests" not in sys.modules:
        requests = types.ModuleType("requests")
        requests.exceptions = types.SimpleNamespace(
            RequestException=Exception, ReadTimeout=TimeoutError
        )
        _install("requests", requests)

    spotipy = types.ModuleType("spotipy")
    spotipy_exceptions = types.ModuleType("spotipy.exceptions")
    spotipy_oauth2 = types.ModuleType("spotipy.oauth2")

    class SpotifyException(Exception):
        def __init__(self, http_status=None, headers=None):
            super().__init__("spotify")
            self.http_status = http_status
            self.headers = headers or {}

    class SpotifyOauthError(Exception):
        pass

    class CacheHandler:
        pass

    class SpotifyOAuth:
        def __init__(self, **_kwargs):
            pass

        def refresh_access_token(self, _refresh_token):
            return {"access_token": "token"}

    class Spotify:
        """A playlist that actually shrinks when tracks are removed."""

        def __init__(self, auth=None, auth_manager=None):
            self.auth = auth
            self.auth_manager = auth_manager
            self.uris = list(uris)
            self.remove_batches: list[list[dict]] = []
            self.playlist_items_calls = 0
            self.playlists_seen: list[str] = []

        def playlist_items(
            self, playlist_id, fields=None, additional_types=None, limit=100, offset=0
        ):
            _ = (fields, additional_types)
            self.playlist_items_calls += 1
            self.playlists_seen.append(playlist_id)
            window = self.uris[offset : offset + limit]
            return {
                "total": len(self.uris),
                "items": [{"track": {"uri": u}} for u in window],
            }

        def playlist_remove_specific_occurrences_of_items(self, playlist_id, items):
            self.playlists_seen.append(playlist_id)
            self.remove_batches.append(list(items))
            positions = {p for item in items for p in item["positions"]}
            self.uris = [u for i, u in enumerate(self.uris) if i not in positions]
            return {"snapshot_id": "r"}

    spotipy.Spotify = Spotify  # type: ignore[attr-defined]
    spotipy.exceptions = spotipy_exceptions  # type: ignore[attr-defined]
    spotipy.oauth2 = spotipy_oauth2  # type: ignore[attr-defined]
    _install("spotipy", spotipy)
    spotipy_exceptions.SpotifyException = SpotifyException  # type: ignore[attr-defined]
    spotipy_exceptions.SpotifyOauthError = SpotifyOauthError  # type: ignore[attr-defined]
    _install("spotipy.exceptions", spotipy_exceptions)
    spotipy_oauth2.CacheHandler = CacheHandler  # type: ignore[attr-defined]
    spotipy_oauth2.SpotifyOAuth = SpotifyOAuth  # type: ignore[attr-defined]
    _install("spotipy.oauth2", spotipy_oauth2)

    mod = importlib.reload(importlib.import_module("mini_app_polis.spotify.spotify"))
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    mod._spotify_api = None
    return mod


def test_trim_playlist_to_limit_removes_old_tracks(monkeypatch):
    mod = _install_stubs(monkeypatch, uris=["uri:old", "uri:keep1", "uri:keep2"])
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=2)

    assert api.client.uris == ["uri:keep1", "uri:keep2"]
    assert api.client.remove_batches == [[{"uri": "uri:old", "positions": [0]}]]


def test_trim_playlist_keeps_going_until_it_is_actually_under_the_limit(monkeypatch):
    """One pass could only ever remove a single page's worth.

    A playlist left untrimmed for months sits thousands of tracks over the
    limit; a single-pass trim could hold it steady but never dig it out.
    """
    mod = _install_stubs(monkeypatch, uris=[f"uri:{i}" for i in range(450)])
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=200)

    assert len(api.client.uris) == 200
    assert [len(batch) for batch in api.client.remove_batches] == [100, 100, 50]
    # The oldest went; the newest stayed.
    assert api.client.uris[0] == "uri:250"
    assert api.client.uris[-1] == "uri:449"


def test_trim_playlist_removes_by_position_not_by_uri(monkeypatch):
    """Removing by URI took every other copy of a repeated track with it."""
    mod = _install_stubs(monkeypatch, uris=["uri:dup", "uri:a", "uri:b", "uri:dup"])
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=3)

    assert api.client.uris == ["uri:a", "uri:b", "uri:dup"]


def test_trim_playlist_accepts_an_explicit_playlist_id(monkeypatch):
    """The caller names the playlist; no env var needed."""
    mod = _install_stubs(
        monkeypatch,
        uris=["uri:old", "uri:keep"],
        configured_playlist=None,
    )
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=1, playlist_id="pl-radio")

    assert set(api.client.playlists_seen) == {"pl-radio"}
    assert api.client.uris == ["uri:keep"]


def test_trim_playlist_falls_back_to_the_configured_playlist(monkeypatch):
    mod = _install_stubs(monkeypatch, uris=["uri:old", "uri:keep"])
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=1)

    assert set(api.client.playlists_seen) == {"pl-main"}


def test_trim_playlist_raises_when_no_playlist_is_available(monkeypatch):
    mod = _install_stubs(monkeypatch, uris=["uri:a"], configured_playlist=None)
    api = mod.SpotifyAPI.from_env()

    with pytest.raises(OSError, match="No playlist to trim"):
        api.trim_playlist_to_limit(limit=0)


def test_trim_playlist_under_limit_removes_nothing(monkeypatch):
    mod = _install_stubs(monkeypatch, uris=["uri:a", "uri:b"])
    api = mod.SpotifyAPI.from_env()

    api.trim_playlist_to_limit(limit=5)

    assert api.client.remove_batches == []
    assert api.client.playlist_items_calls == 1
