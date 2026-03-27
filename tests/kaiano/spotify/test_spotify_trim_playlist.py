import importlib
import sys
import types


def _install(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


def test_trim_playlist_to_limit_removes_old_tracks(monkeypatch):
    # Ensure stubbed config
    cfg = sys.modules.get("kaiano.config")
    if cfg is None:
        cfg = types.ModuleType("kaiano.config")
        _install("kaiano.config", cfg)
    cfg.SPOTIPY_CLIENT_ID = "cid"
    cfg.SPOTIPY_CLIENT_SECRET = "secret"
    cfg.SPOTIPY_REFRESH_TOKEN = "refresh"
    cfg.SPOTIPY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
    cfg.SPOTIFY_PLAYLIST_ID = "pl-main"

    # requests + spotipy stubs (minimal)
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
        def __init__(self, auth=None, auth_manager=None):
            self.auth = auth
            self.auth_manager = auth_manager
            self.removed = []

        def playlist_items(
            self, playlist_id, fields=None, additional_types=None, limit=100, offset=0
        ):
            assert playlist_id == "pl-main"
            _ = (fields, additional_types, limit, offset)
            # total=3, remove first (oldest) when limit=2
            return {
                "total": 3,
                "items": [
                    {"track": {"uri": "uri:old"}},
                    {"track": {"uri": "uri:keep1"}},
                    {"track": {"uri": "uri:keep2"}},
                ],
            }

        def playlist_remove_all_occurrences_of_items(self, playlist_id, items):
            _ = playlist_id
            self.removed = list(items)
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

    mod = importlib.reload(importlib.import_module("kaiano.spotify.spotify"))
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)

    api = mod.SpotifyAPI.from_env()
    api.trim_playlist_to_limit(limit=2)

    assert api.client.removed == ["uri:old"]
