import importlib
import sys
import types

import pytest


def _install(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


def _ensure_stubbed_config():
    cfg = sys.modules.get("mini_app_polis.config")
    if cfg is None:
        cfg = types.ModuleType("mini_app_polis.config")
        _install("mini_app_polis.config", cfg)
    # minimal config surface used by spotify.py
    cfg.SPOTIPY_CLIENT_ID = "cid"
    cfg.SPOTIPY_CLIENT_SECRET = "secret"
    cfg.SPOTIPY_REFRESH_TOKEN = "refresh"
    cfg.SPOTIPY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
    cfg.LOGGING_LEVEL = "DEBUG"
    cfg.SPOTIFY_PLAYLIST_ID = "stub-playlist-id"


def test_spotify_retry_helpers_and_client_from_refresh(monkeypatch):
    _ensure_stubbed_config()

    # requests stub
    requests = types.ModuleType("requests")
    exc_mod = types.SimpleNamespace(
        RequestException=Exception, ReadTimeout=TimeoutError
    )
    requests.exceptions = exc_mod
    _install("requests", requests)

    # spotipy stub
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
            self.calls = 0

        def refresh_access_token(self, refresh_token):
            _ = refresh_token
            self.calls += 1
            return {"access_token": f"token-{self.calls}"}

    class Spotify:
        def __init__(self, auth=None, auth_manager=None):
            self.auth = auth
            self.auth_manager = auth_manager
            self._search_calls = 0

        def search(self, q, type, limit):
            _ = (q, type, limit)
            self._search_calls += 1
            return {
                "tracks": {
                    "items": [{"uri": "uri:1", "name": "T", "artists": [{"name": "A"}]}]
                }
            }

        def current_user(self):
            return {"id": "me"}

        def user_playlist_create(self, user, name, public, description):
            _ = (user, name, public, description)
            return {"id": "pl"}

        def playlist_items(self, playlist_id, offset=0, fields=None, **_kwargs):
            _ = (playlist_id, fields)
            # pagination fixture: first page has one existing track, then ends
            if offset == 0:
                return {
                    "items": [{"track": {"uri": "uri:existing"}}],
                    "total": 1,
                    "next": None,
                }
            return {"items": [], "total": 0, "next": None}

        def playlist_add_items(self, playlist_id, items):
            _ = (playlist_id, items)
            return {"snapshot_id": "s"}

        def current_user_playlists(self, limit=50, offset=0):
            _ = limit
            if offset:
                return {"items": [{"name": "MyPlaylist", "id": "pl-1"}], "next": None}
            other = [{"name": f"Other {i}", "id": f"o-{i}"} for i in range(50)]
            return {"items": other, "next": "more"}

        def playlist_remove_all_occurrences_of_items(self, playlist_id, items):
            _ = playlist_id
            self._removed = list(items)
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

    # Import module under test (reload to pick up stubs)
    mod = importlib.reload(importlib.import_module("mini_app_polis.spotify.spotify"))

    # avoid real sleeps
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)

    api = mod.SpotifyAPI.from_env()
    client = api.client
    assert isinstance(client, Spotify)
    assert client.auth.startswith("token-")

    # basic facade methods exercise call path
    assert api.search_track("A", "T") == "uri:1"
    assert api.create_playlist("Name", "Desc") == "pl"

    # add_tracks_to_specific_playlist: filters existing unless allowDuplicates=True
    api.add_tracks_to_specific_playlist(
        "pl", ["uri:existing", "uri:new"], allowDuplicates=False
    )

    # get_playlist_tracks: returns URIs
    assert api.get_playlist_tracks("pl") == ["uri:existing"]

    # find_playlist_by_name (match)
    assert api.find_playlist_by_name("MyPlaylist")["id"] == "pl-1"

    # retry helpers
    assert (
        mod._is_retryable_spotify_exception(SpotifyException(http_status=429)) is True
    )
    assert (
        mod._is_retryable_spotify_exception(SpotifyException(http_status=503)) is True
    )
    assert (
        mod._is_retryable_spotify_exception(SpotifyException(http_status=400)) is False
    )


def test_spotify_call_with_retry_respects_rate_limit(monkeypatch):
    _ensure_stubbed_config()

    # Ensure our third-party stubs exist (tests can run in any order).
    if "spotipy" not in sys.modules:
        # Minimal repeat of the stubs from the first test.
        requests = types.ModuleType("requests")
        exc_mod = types.SimpleNamespace(
            RequestException=Exception, ReadTimeout=TimeoutError
        )
        requests.exceptions = exc_mod
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

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))

    class E(mod.SpotifyException):
        pass

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise E(http_status=429, headers={"Retry-After": "3"})
        return "ok"

    out = mod.SpotifyAPI()._call_with_retry(fn, context="testing")
    assert out == "ok"
    # slept for retry-after
    assert sleeps and sleeps[0] >= 1


def _install_clear_playlist_stubs(monkeypatch):
    """Minimal spotipy/requests stubs for clear_playlist tests."""
    _ensure_stubbed_config()

    requests = types.ModuleType("requests")
    exc_mod = types.SimpleNamespace(
        RequestException=Exception, ReadTimeout=TimeoutError
    )
    requests.exceptions = exc_mod
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
            self.calls = 0

        def refresh_access_token(self, refresh_token):
            _ = refresh_token
            self.calls += 1
            return {"access_token": f"token-{self.calls}"}

    class Spotify:
        def __init__(self, auth=None, auth_manager=None):
            self.auth = auth
            self.auth_manager = auth_manager
            self.remove_calls: list[tuple[str, list[str]]] = []

        def playlist_remove_all_occurrences_of_items(self, playlist_id, items):
            self.remove_calls.append((playlist_id, list(items)))
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
    return mod, Spotify


class TestClearPlaylist:
    def test_clears_successfully_single_batch(self, monkeypatch):
        mod, SpotifyCls = _install_clear_playlist_stubs(monkeypatch)
        api = mod.SpotifyAPI.from_env()
        client = api.client
        assert isinstance(client, SpotifyCls)
        monkeypatch.setattr(
            api,
            "playlist_track_uris",
            lambda _pid: ["u1", "u2", "u3"],
        )

        api.clear_playlist("pl-1")

        assert len(client.remove_calls) == 1
        assert client.remove_calls[0] == ("pl-1", ["u1", "u2", "u3"])

    def test_batches_at_100(self, monkeypatch):
        mod, SpotifyCls = _install_clear_playlist_stubs(monkeypatch)
        api = mod.SpotifyAPI.from_env()
        client = api.client
        assert isinstance(client, SpotifyCls)
        uris = [f"uri:{i}" for i in range(101)]
        monkeypatch.setattr(api, "playlist_track_uris", lambda _pid: uris)

        api.clear_playlist("big-pl")

        assert len(client.remove_calls) == 2
        assert len(client.remove_calls[0][1]) == 100
        assert client.remove_calls[0][0] == "big-pl"
        assert len(client.remove_calls[1][1]) == 1
        assert client.remove_calls[1][0] == "big-pl"

    def test_no_op_when_empty(self, monkeypatch):
        mod, SpotifyCls = _install_clear_playlist_stubs(monkeypatch)
        api = mod.SpotifyAPI.from_env()
        client = api.client
        assert isinstance(client, SpotifyCls)
        monkeypatch.setattr(api, "playlist_track_uris", lambda _pid: [])

        api.clear_playlist("empty-pl")

        assert client.remove_calls == []

    def test_raises_when_the_tracks_cannot_be_read(self, monkeypatch):
        """Not "already empty": a refill would double the playlist."""
        mod, SpotifyCls = _install_clear_playlist_stubs(monkeypatch)
        api = mod.SpotifyAPI.from_env()
        client = api.client
        assert isinstance(client, SpotifyCls)

        def boom(_pid):
            raise RuntimeError("network")

        monkeypatch.setattr(api, "playlist_track_uris", boom)

        with pytest.raises(RuntimeError, match="network"):
            api.clear_playlist("pl-x")

        assert client.remove_calls == []

    def test_get_playlist_tracks_still_answers_empty_on_failure(self, monkeypatch):
        mod, _ = _install_clear_playlist_stubs(monkeypatch)
        api = mod.SpotifyAPI.from_env()

        def boom(_pid):
            raise RuntimeError("network")

        monkeypatch.setattr(api, "playlist_track_uris", boom)
        # Other suites leave a stub logger behind; this one takes exc_info.
        monkeypatch.setattr(
            mod, "log", types.SimpleNamespace(error=lambda *_a, **_k: None)
        )

        assert api.get_playlist_tracks("pl-x") == []

    def test_module_clear_playlist_delegates(self, monkeypatch):
        mod, _ = _install_clear_playlist_stubs(monkeypatch)
        called: list[str] = []

        class FakeAPI:
            def clear_playlist(self, playlist_id: str) -> None:
                called.append(playlist_id)

        monkeypatch.setattr(mod, "_get_api", lambda: FakeAPI())

        mod.clear_playlist("pl-delegated")

        assert called == ["pl-delegated"]


def _install_trim_playlist_stubs(monkeypatch, *, total: int):
    """Minimal spotipy/requests stubs for trim_playlist_to_limit tests."""
    _ensure_stubbed_config()

    requests = types.ModuleType("requests")
    exc_mod = types.SimpleNamespace(
        RequestException=Exception, ReadTimeout=TimeoutError
    )
    requests.exceptions = exc_mod
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
            self.calls = 0

        def refresh_access_token(self, refresh_token):
            _ = refresh_token
            self.calls += 1
            return {"access_token": f"token-{self.calls}"}

    class Spotify:
        def __init__(self, auth=None, auth_manager=None):
            self.auth = auth
            self.auth_manager = auth_manager
            self.playlist_items_calls = 0
            self.remove_calls = 0

        def playlist_items(
            self, playlist_id, fields=None, additional_types=None, limit=100, offset=0
        ):
            _ = (playlist_id, fields, additional_types, limit, offset)
            self.playlist_items_calls += 1
            items = [{"track": {"uri": f"uri:{i}"}} for i in range(total)]
            return {"total": total, "items": items}

        def playlist_remove_specific_occurrences_of_items(self, playlist_id, items):
            _ = (playlist_id, items)
            self.remove_calls += 1
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
    return mod, Spotify


def test_spotify_client_raises_when_client_id_missing(monkeypatch):
    _ensure_stubbed_config()
    cfg = sys.modules["mini_app_polis.config"]
    cfg.SPOTIPY_CLIENT_ID = None

    requests = types.ModuleType("requests")
    requests.exceptions = types.SimpleNamespace(
        RequestException=Exception, ReadTimeout=TimeoutError
    )
    _install("requests", requests)

    spotipy = types.ModuleType("spotipy")
    spotipy_exceptions = types.ModuleType("spotipy.exceptions")
    spotipy_oauth2 = types.ModuleType("spotipy.oauth2")

    class SpotifyException(Exception):
        pass

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

    api = mod.SpotifyAPI.from_env()
    try:
        _ = api.client
    except ValueError as e:
        assert "Missing one or more required Spotify credentials." in str(e)
    else:
        raise AssertionError("expected ValueError for missing SPOTIPY_CLIENT_ID")


def test_trim_playlist_to_limit_under_limit_makes_no_remove_calls(monkeypatch):
    mod, SpotifyCls = _install_trim_playlist_stubs(monkeypatch, total=2)
    api = mod.SpotifyAPI.from_env()
    client = api.client
    assert isinstance(client, SpotifyCls)

    api.trim_playlist_to_limit(limit=5)

    assert client.playlist_items_calls == 1
    assert client.remove_calls == 0


def test_trim_playlist_to_limit_empty_playlist_makes_no_remove_calls(monkeypatch):
    mod, SpotifyCls = _install_trim_playlist_stubs(monkeypatch, total=0)
    api = mod.SpotifyAPI.from_env()
    client = api.client
    assert isinstance(client, SpotifyCls)

    api.trim_playlist_to_limit(limit=5)

    assert client.playlist_items_calls == 1
    assert client.remove_calls == 0


# ---------------------------------------------------------------------------
# Facade behaviour against a MagicMock client.
#
# Rather than reinstalling stub packages and reloading, these patch the names
# spotify.py looks up at call time (its exception classes, ``requests``,
# ``config``, ``log`` and ``time``), so they hold whichever spotipy the module
# was last imported against.
# ---------------------------------------------------------------------------


class _SpotifyError(Exception):
    def __init__(self, http_status=None, headers=None):
        super().__init__(f"spotify {http_status}")
        self.http_status = http_status
        self.headers = headers


class _OauthError(Exception):
    pass


class _ReadTimeout(Exception):
    pass


class _RequestException(Exception):
    pass


@pytest.fixture
def sp(monkeypatch):
    """(module, api, client, sleeps, log) with the client already attached."""
    from unittest.mock import MagicMock

    mod = importlib.import_module("mini_app_polis.spotify.spotify")
    sleeps: list[float] = []
    log = MagicMock()
    monkeypatch.setattr(mod, "SpotifyException", _SpotifyError)
    monkeypatch.setattr(mod, "SpotifyOauthError", _OauthError)
    monkeypatch.setattr(
        mod,
        "requests",
        types.SimpleNamespace(
            exceptions=types.SimpleNamespace(
                ReadTimeout=_ReadTimeout, RequestException=_RequestException
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "config",
        types.SimpleNamespace(
            SPOTIPY_CLIENT_ID="cid",
            SPOTIPY_CLIENT_SECRET="secret",
            SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback",
            SPOTIPY_REFRESH_TOKEN=None,
            SPOTIFY_PLAYLIST_ID="default-pl",
        ),
    )
    monkeypatch.setattr(mod, "log", log)
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(mod, "_spotify_api", None)

    api = mod.SpotifyAPI()
    client = MagicMock()
    api._client = client
    return mod, api, client, sleeps, log


# --- _call_with_retry ------------------------------------------------------


def test_call_with_retry_backs_off_on_a_server_error_then_succeeds(sp):
    mod, api, _, sleeps, _ = sp
    outcomes = [_SpotifyError(502), "ok"]

    def fn():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    assert api._call_with_retry(fn, context="t") == "ok"
    assert sleeps == [2]  # base 2s x attempt 1


def test_call_with_retry_raises_a_server_error_on_the_last_attempt(sp):
    _, api, _, sleeps, _ = sp
    calls = []

    def fn():
        calls.append(1)
        raise _SpotifyError(503)

    with pytest.raises(_SpotifyError):
        api._call_with_retry(fn, context="t", max_retries=3)
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_call_with_retry_does_not_retry_a_client_error(sp):
    _, api, _, sleeps, _ = sp
    calls = []

    def fn():
        calls.append(1)
        raise _SpotifyError(404)

    with pytest.raises(_SpotifyError):
        api._call_with_retry(fn, context="t")
    assert calls == [1]
    assert sleeps == []


def test_call_with_retry_retries_a_read_timeout_then_raises(sp):
    _, api, _, sleeps, _ = sp
    calls = []

    def fn():
        calls.append(1)
        raise _ReadTimeout("slow")

    with pytest.raises(_ReadTimeout):
        api._call_with_retry(fn, context="t", max_retries=2)
    assert len(calls) == 2
    assert sleeps == [2]


@pytest.mark.parametrize(
    ("headers", "expected_sleep"),
    [
        ({"Retry-After": "7"}, 7),
        ({"Retry-After": "0"}, 1),  # never less than a second
        ({"Retry-After": "soon"}, 2),  # unparseable: the default
        (None, 2),
    ],
)
def test_rate_limit_sleeps_for_retry_after(sp, headers, expected_sleep):
    _, api, _, sleeps, _ = sp
    outcomes = [_SpotifyError(429, headers), "ok"]

    def fn():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    assert api._call_with_retry(fn, context="t") == "ok"
    assert sleeps == [expected_sleep]


def test_call_with_retry_raises_when_rate_limited_on_every_attempt(sp):
    # Exhausting the retries on 429 used to fall out of the loop and return
    # None, which callers then treated as a Spotify response.
    _, api, _, sleeps, _ = sp
    calls = []

    def fn():
        calls.append(1)
        raise _SpotifyError(429, {"Retry-After": "5"})

    with pytest.raises(_SpotifyError) as exc:
        api._call_with_retry(fn, context="t", max_retries=3)
    assert exc.value.http_status == 429
    assert len(calls) == 3
    assert sleeps == [5, 5]  # no sleep after the last attempt


def test_search_track_returns_none_when_rate_limited_throughout(sp):
    _, api, client, _, log = sp
    client.search.side_effect = _SpotifyError(429, {"Retry-After": "1"})

    assert api.search_track("A", "T") is None
    assert client.search.call_count == 3
    log.error.assert_called_once()


def test_create_playlist_does_not_create_for_no_user_when_rate_limited(sp):
    _, api, client, _, _ = sp
    client.current_user.side_effect = _SpotifyError(429, {"Retry-After": "1"})

    assert api.create_playlist("Name", "Desc") is None
    client.user_playlist_create.assert_not_called()


# --- client / token refresh ------------------------------------------------


def test_client_uses_interactive_oauth_without_a_refresh_token(sp, monkeypatch):
    from unittest.mock import MagicMock

    mod, _, _, _, _ = sp
    oauth = MagicMock(return_value="auth-manager")
    spotipy_mod = types.SimpleNamespace(Spotify=MagicMock(return_value="client"))
    monkeypatch.setattr(mod, "SpotifyOAuth", oauth)
    monkeypatch.setattr(mod, "spotipy", spotipy_mod)

    api = mod.SpotifyAPI()
    assert api.client == "client"
    assert api.client == "client"  # cached

    oauth.assert_called_once_with(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-modify-public playlist-modify-private",
        cache_path=".cache-ci",
        open_browser=False,
    )
    spotipy_mod.Spotify.assert_called_once_with(auth_manager="auth-manager")


def _refreshing(mod, monkeypatch, refresh_outcomes):
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.refresh_access_token.side_effect = refresh_outcomes
    oauth = MagicMock(return_value=manager)
    spotify_cls = MagicMock(side_effect=lambda auth: ("client", auth))
    monkeypatch.setattr(mod, "SpotifyOAuth", oauth)
    monkeypatch.setattr(mod, "Spotify", spotify_cls)
    mod.config.SPOTIPY_REFRESH_TOKEN = "refresh-tok"
    return manager, oauth


def test_token_refresh_retries_transient_failures(sp, monkeypatch):
    mod, _, _, sleeps, _ = sp
    manager, oauth = _refreshing(
        mod,
        monkeypatch,
        [
            _OauthError("bad gateway"),
            _RequestException("reset"),
            {"access_token": "AT"},
        ],
    )

    assert mod.SpotifyAPI().client == ("client", "AT")
    assert manager.refresh_access_token.call_count == 3
    manager.refresh_access_token.assert_called_with("refresh-tok")
    assert sleeps == [2, 4]
    # Refresh flow never persists a token cache.
    assert isinstance(oauth.call_args.kwargs["cache_handler"], mod.NoopCacheHandler)


def test_token_refresh_gives_up_after_three_attempts(sp, monkeypatch):
    mod, _, _, sleeps, log = sp
    manager, _ = _refreshing(mod, monkeypatch, [_OauthError("no")] * 3)

    with pytest.raises(_OauthError):
        _ = mod.SpotifyAPI().client
    assert manager.refresh_access_token.call_count == 3
    assert sleeps == [2, 4]
    log.error.assert_called_once()


def test_token_refresh_does_not_retry_an_unexpected_error(sp, monkeypatch):
    mod, _, _, sleeps, _ = sp
    manager, _ = _refreshing(mod, monkeypatch, [KeyError("access_token")])

    with pytest.raises(KeyError):
        _ = mod.SpotifyAPI().client
    assert manager.refresh_access_token.call_count == 1
    assert sleeps == []


def test_noop_cache_handler_never_reads_or_writes():
    mod = importlib.import_module("mini_app_polis.spotify.spotify")
    handler = mod.NoopCacheHandler()

    handler.save_token_to_cache({"access_token": "x"})
    assert handler.get_cached_token() is None


# --- search_track ----------------------------------------------------------


def test_search_track_queries_by_artist_and_title(sp):
    _, api, client, _, log = sp
    client.search.return_value = {
        "tracks": {
            "items": [
                {
                    "uri": "spotify:track:1",
                    "name": "Song",
                    "artists": [{"name": "Band"}],
                }
            ]
        }
    }

    assert api.search_track("band", "song") == "spotify:track:1"
    client.search.assert_called_once_with(
        q="artist:band track:song", type="track", limit=1
    )
    # Same track up to case: no mismatch warning.
    log.warning.assert_not_called()


def test_search_track_warns_when_the_match_differs(sp):
    _, api, client, _, log = sp
    client.search.return_value = {
        "tracks": {"items": [{"uri": "spotify:track:2", "name": "Other"}]}
    }

    assert api.search_track("Band", "Song") == "spotify:track:2"
    warnings = [c.args[0] for c in log.warning.call_args_list]
    assert any("Band - Song" in w for w in warnings)
    assert any("Unknown Artist - Other" in w for w in warnings)


def test_search_track_returns_none_when_nothing_is_found(sp):
    _, api, client, _, _ = sp
    client.search.return_value = {"tracks": {"items": []}}

    assert api.search_track("A", "T") is None


def test_search_track_returns_none_when_spotify_fails(sp):
    _, api, client, _, log = sp
    client.search.side_effect = _SpotifyError(400)

    assert api.search_track("A", "T") is None
    log.error.assert_called_once()


# --- create_playlist -------------------------------------------------------


def test_create_playlist_creates_a_public_playlist_for_the_current_user(sp):
    _, api, client, _, _ = sp
    client.current_user.return_value = {"id": "me"}
    client.user_playlist_create.return_value = {"id": "pl-9"}

    assert api.create_playlist("Name", "Desc") == "pl-9"
    client.user_playlist_create.assert_called_once_with(
        user="me", name="Name", public=True, description="Desc"
    )


def test_create_playlist_returns_none_on_failure(sp):
    _, api, client, _, _ = sp
    client.current_user.return_value = {"id": "me"}
    client.user_playlist_create.side_effect = _SpotifyError(403)

    assert api.create_playlist("Name", "Desc") is None


# --- add_tracks_to_specific_playlist ---------------------------------------


def test_add_tracks_requires_a_playlist_id(sp):
    _, api, client, _, _ = sp

    with pytest.raises(ValueError, match="Missing playlist_id"):
        api.add_tracks_to_specific_playlist("", ["u1"])
    client.playlist_add_items.assert_not_called()


def test_add_tracks_with_no_uris_does_nothing(sp):
    _, api, client, _, _ = sp

    api.add_tracks_to_specific_playlist("pl", [])

    client.playlist_items.assert_not_called()
    client.playlist_add_items.assert_not_called()


def test_add_tracks_skips_tracks_already_in_any_page_of_the_playlist(sp):
    _, api, client, _, _ = sp
    client.playlist_items.side_effect = [
        {"items": [{"track": {"uri": "u1"}}, {"track": None}], "next": "page2"},
        {"items": [{"track": {"uri": "u3"}}, {"track": {}}], "next": None},
    ]

    api.add_tracks_to_specific_playlist("pl", ["u1", "u2", "u2", "u3", "u4"])

    assert [c.kwargs["offset"] for c in client.playlist_items.call_args_list] == [
        0,
        100,
    ]
    # Input is de-duplicated in order, and existing tracks are dropped.
    client.playlist_add_items.assert_called_once_with("pl", ["u2", "u4"])


def test_add_tracks_makes_no_add_call_when_all_are_present(sp):
    _, api, client, _, _ = sp
    client.playlist_items.return_value = {
        "items": [{"track": {"uri": "u1"}}],
        "next": None,
    }

    api.add_tracks_to_specific_playlist("pl", ["u1"])

    client.playlist_add_items.assert_not_called()


def test_add_tracks_allowing_duplicates_skips_the_playlist_read(sp):
    _, api, client, _, _ = sp

    api.add_tracks_to_specific_playlist("pl", ["u1", "u1", "u2"], allowDuplicates=True)

    client.playlist_items.assert_not_called()
    client.playlist_add_items.assert_called_once_with("pl", ["u1", "u2"])


# --- playlist_track_uris / find_playlist_by_name ---------------------------


def test_playlist_track_uris_pages_until_there_is_no_next(sp):
    _, api, client, _, _ = sp
    client.playlist_items.side_effect = [
        {"items": [{"track": {"uri": "a"}}, {"track": None}], "next": "more"},
        {"items": None, "next": "more"},
        {"items": [{"track": {"uri": "b"}}], "next": None},
    ]

    assert api.playlist_track_uris("pl") == ["a", "b"]
    assert [c.kwargs["offset"] for c in client.playlist_items.call_args_list] == [
        0,
        100,
        200,
    ]


def test_playlist_track_uris_without_an_id_is_empty(sp):
    _, api, client, _, _ = sp

    assert api.playlist_track_uris("") == []
    client.playlist_items.assert_not_called()


def test_find_playlist_by_name_returns_none_after_reading_every_page(sp):
    _, api, client, _, _ = sp
    client.current_user_playlists.side_effect = [
        {"items": [{"name": "A", "id": "1"}], "next": "more"},
        {"items": [{"name": "B", "id": "2"}], "next": None},
    ]

    assert api.find_playlist_by_name("Missing") is None
    assert [c.kwargs for c in client.current_user_playlists.call_args_list] == [
        {"limit": 50, "offset": 0},
        {"limit": 50, "offset": 50},
    ]


def test_find_playlist_by_name_raises_when_spotify_does(sp):
    """'Could not look' must not read as 'not there', or a duplicate gets made."""
    _, api, client, _, _ = sp
    client.current_user_playlists.side_effect = _SpotifyError(401)

    with pytest.raises(_SpotifyError):
        api.find_playlist_by_name("A")


def test_find_playlist_by_name_uses_a_fresh_refresh_token_client(sp, monkeypatch):
    from unittest.mock import MagicMock

    mod, api, cached, _, _ = sp
    mod.config.SPOTIPY_REFRESH_TOKEN = "refresh-tok"
    fresh = MagicMock()
    fresh.current_user_playlists.return_value = {
        "items": [{"name": "A", "id": "1"}],
        "next": None,
    }
    monkeypatch.setattr(api, "_client_from_refresh", lambda: fresh)

    assert api.find_playlist_by_name("A") == {
        "id": "1",
        "data": {"name": "A", "id": "1"},
    }
    cached.current_user_playlists.assert_not_called()


# --- trim_playlist_to_limit edge cases -------------------------------------


def test_trim_stops_when_a_removal_does_not_shrink_the_playlist(sp):
    _, api, client, _, log = sp
    client.playlist_items.return_value = {
        "total": 3,
        "items": [{"track": {"uri": f"u{i}"}} for i in range(3)],
    }

    api.trim_playlist_to_limit(limit=1, playlist_id="pl")

    # One removal, then the unchanged total stops the loop.
    client.playlist_remove_specific_occurrences_of_items.assert_called_once_with(
        "pl",
        [{"uri": "u0", "positions": [0]}, {"uri": "u1", "positions": [1]}],
    )
    assert "did not fall" in log.warning.call_args.args[0]


def test_trim_removes_nothing_when_the_oldest_items_have_no_uri(sp):
    _, api, client, _, log = sp
    client.playlist_items.return_value = {
        "total": 3,
        "items": [{"track": None}, "junk", {"track": {"uri": "u2"}}],
    }

    api.trim_playlist_to_limit(limit=1, playlist_id="pl")

    client.playlist_remove_specific_occurrences_of_items.assert_not_called()
    assert "carry no track URIs" in log.warning.call_args.args[0]


# --- module-level helpers --------------------------------------------------


def test_module_helpers_share_one_facade(sp, monkeypatch):
    from unittest.mock import MagicMock

    mod, _, _, _, _ = sp
    fake = MagicMock()
    fake.client = "the-client"
    fake.search_track.return_value = "uri"
    fake.create_playlist.return_value = "pl"
    fake.find_playlist_by_name.return_value = {"id": "pl"}
    fake.get_playlist_tracks.return_value = ["u"]
    monkeypatch.setattr(mod.SpotifyAPI, "from_env", staticmethod(lambda: fake))

    assert mod.get_spotify_client() == "the-client"
    assert mod.search_track("A", "T") == "uri"
    assert mod.create_playlist("N", "D") == "pl"
    assert mod.find_playlist_by_name("N") == {"id": "pl"}
    assert mod.get_playlist_tracks("pl") == ["u"]
    mod.trim_playlist_to_limit(50)
    mod.add_tracks_to_playlist(["u1"], allowDuplicates=True)
    mod.add_tracks_to_specific_playlist("other", ["u2"])
    mod.get_spotify_client_from_refresh()

    fake.search_track.assert_called_once_with("A", "T")
    fake.create_playlist.assert_called_once_with("N", "D")
    fake.trim_playlist_to_limit.assert_called_once_with(50)
    assert fake.add_tracks_to_specific_playlist.call_args_list == [
        (("default-pl", ["u1"]), {"allowDuplicates": True}),
        (("other", ["u2"]), {"allowDuplicates": False}),
    ]
    fake._client_from_refresh.assert_called_once_with()
    assert mod._spotify_api is fake


def test_get_spotify_client_from_refresh_builds_the_facade_on_first_use(
    sp, monkeypatch
):
    from unittest.mock import MagicMock

    mod, _, _, _, _ = sp
    fake = MagicMock()
    fake._client_from_refresh.return_value = "fresh"
    monkeypatch.setattr(mod.SpotifyAPI, "from_env", staticmethod(lambda: fake))

    assert mod.get_spotify_client_from_refresh() == "fresh"
    assert mod._spotify_api is fake


def test_add_tracks_to_playlist_requires_the_configured_playlist(sp):
    mod, _, _, _, _ = sp
    mod.config.SPOTIFY_PLAYLIST_ID = None

    with pytest.raises(ValueError, match="SPOTIFY_PLAYLIST_ID is not set"):
        mod.add_tracks_to_playlist(["u1"])
