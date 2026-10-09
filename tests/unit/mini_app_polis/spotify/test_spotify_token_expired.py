"""invalid_grant on refresh: no retries, and SpotifyTokenExpired instead."""

import importlib
import sys
import types

import pytest


class _OauthError(Exception):
    """spotipy's SpotifyOauthError: the OAuth code is on ``.error``."""

    def __init__(self, message, error=None, error_description=None):
        super().__init__(message)
        self.error = error
        self.error_description = error_description


@pytest.fixture
def spotify(monkeypatch):
    """The spotify module over stubbed spotipy, with a scripted token endpoint."""
    cfg = types.ModuleType("mini_app_polis.config")
    cfg.SPOTIPY_CLIENT_ID = "cid"
    cfg.SPOTIPY_CLIENT_SECRET = "secret"
    cfg.SPOTIPY_REFRESH_TOKEN = "the-refresh-token"
    cfg.SPOTIPY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
    cfg.SPOTIFY_PLAYLIST_ID = "pl"
    monkeypatch.setitem(sys.modules, "mini_app_polis.config", cfg)

    requests = types.ModuleType("requests")
    requests.exceptions = types.SimpleNamespace(
        RequestException=OSError, ReadTimeout=TimeoutError
    )
    monkeypatch.setitem(sys.modules, "requests", requests)

    outcomes: list = []
    calls = {"refresh": 0}

    class SpotifyOAuth:
        def __init__(self, **_kwargs):
            pass

        def refresh_access_token(self, _token):
            calls["refresh"] += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class Spotify:
        def __init__(self, auth=None, **_kwargs):
            self.auth = auth

    spotipy = types.ModuleType("spotipy")
    exc = types.ModuleType("spotipy.exceptions")
    oauth2 = types.ModuleType("spotipy.oauth2")
    exc.SpotifyException = type("SpotifyException", (Exception,), {})
    exc.SpotifyOauthError = _OauthError
    oauth2.CacheHandler = object
    oauth2.SpotifyOAuth = SpotifyOAuth
    spotipy.Spotify = Spotify
    spotipy.exceptions = exc
    spotipy.oauth2 = oauth2
    for name, mod in (
        ("spotipy", spotipy),
        ("spotipy.exceptions", exc),
        ("spotipy.oauth2", oauth2),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    mod = importlib.reload(importlib.import_module("mini_app_polis.spotify.spotify"))
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    yield types.SimpleNamespace(mod=mod, outcomes=outcomes, calls=calls)
    monkeypatch.undo()
    importlib.reload(sys.modules["mini_app_polis.spotify.spotify"])


def test_invalid_grant_raises_token_expired_without_retrying(spotify, caplog):
    spotify.outcomes.append(
        _OauthError(
            "error: invalid_grant, error_description: Refresh token revoked",
            error="invalid_grant",
            error_description="Refresh token revoked",
        )
    )

    with pytest.raises(spotify.mod.SpotifyTokenExpired) as exc:
        _ = spotify.mod.SpotifyAPI().client

    assert spotify.calls["refresh"] == 1
    assert "the-refresh-token" not in str(exc.value)
    assert "the-refresh-token" not in caplog.text
    assert exc.value.__cause__ is None


def test_invalid_grant_only_in_the_message_still_counts(spotify):
    spotify.outcomes.append(_OauthError("error: invalid_grant"))

    with pytest.raises(spotify.mod.SpotifyTokenExpired):
        _ = spotify.mod.SpotifyAPI().client
    assert spotify.calls["refresh"] == 1


def test_other_oauth_errors_are_still_retried(spotify):
    spotify.outcomes += [
        _OauthError("error: server_error", error="server_error"),
        {"access_token": "fresh"},
    ]

    client = spotify.mod.SpotifyAPI().client

    assert client.auth == "fresh"
    assert spotify.calls["refresh"] == 2


def test_other_oauth_errors_give_up_as_themselves(spotify):
    spotify.outcomes += [_OauthError("x", error="invalid_client")] * 3

    with pytest.raises(_OauthError):
        _ = spotify.mod.SpotifyAPI().client
    assert spotify.calls["refresh"] == 3


def test_the_package_exports_it():
    from mini_app_polis.spotify import SpotifyTokenExpired

    assert issubclass(SpotifyTokenExpired, RuntimeError)
