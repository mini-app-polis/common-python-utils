from __future__ import annotations

import time

import requests
import spotipy
from spotipy import Spotify
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import CacheHandler, SpotifyOAuth

from kaiano import config
from kaiano import logger as log

log = log.get_logger()


# --- Centralized retry helpers ---


def _sleep_backoff(attempt: int, base_seconds: int = 2) -> None:
    time.sleep(base_seconds * attempt)


def _is_retryable_spotify_exception(e: SpotifyException) -> bool:
    status = e.http_status
    return status == 429 or (status is not None and status >= 500)


def _sleep_for_rate_limit(e: SpotifyException, default_seconds: int = 2) -> None:
    try:
        retry_after = int((e.headers or {}).get("Retry-After", default_seconds))
    except Exception:
        retry_after = default_seconds
    time.sleep(max(1, retry_after))


_spotify_api: SpotifyAPI | None = None


class NoopCacheHandler(CacheHandler):
    def get_cached_token(self):
        return None

    def save_token_to_cache(self, token_info):
        pass


# --- SpotifyAPI facade class ---


class SpotifyAPI:
    """Single entry point for Spotify operations.

    This mirrors the pattern used by GoogleAPI: one object owns auth/client and provides
    stable methods with consistent retry behavior.
    """

    def __init__(self):
        self._client: Spotify | None = None

    @classmethod
    def from_env(cls) -> SpotifyAPI:
        return cls()

    @property
    def client(self) -> Spotify:
        if self._client is not None:
            return self._client

        if config.SPOTIPY_REFRESH_TOKEN:
            log.debug("🔄 Using refresh-token authentication.")
            self._client = self._client_from_refresh()
        else:
            log.debug("⚙️ Using OAuth (local interactive) authentication.")
            self._client = spotipy.Spotify(
                auth_manager=SpotifyOAuth(
                    client_id=config.SPOTIPY_CLIENT_ID,
                    client_secret=config.SPOTIPY_CLIENT_SECRET,
                    redirect_uri=config.SPOTIPY_REDIRECT_URI,
                    scope="playlist-modify-public playlist-modify-private",
                    cache_path=".cache-ci",
                    open_browser=False,
                )
            )

        return self._client

    def _client_from_refresh(self) -> Spotify:
        client_id = config.SPOTIPY_CLIENT_ID
        client_secret = config.SPOTIPY_CLIENT_SECRET
        redirect_uri = config.SPOTIPY_REDIRECT_URI
        refresh_token = config.SPOTIPY_REFRESH_TOKEN

        log.debug(
            f"Loaded env vars: "
            f"client_id={'set' if client_id else 'unset'}, "
            f"client_secret={'set' if client_secret else 'unset'}, "
            f"redirect_uri={'set' if redirect_uri else 'unset'}, "
            f"refresh_token={'set' if refresh_token else 'unset'}"
        )

        if not all([client_id, client_secret, redirect_uri, refresh_token]):
            log.critical("Missing one or more required Spotify credentials.")
            raise ValueError("Missing one or more required Spotify credentials.")

        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private",
            cache_handler=NoopCacheHandler(),
        )

        max_retries = 3
        base_backoff_seconds = 2

        for attempt in range(1, max_retries + 1):
            try:
                log.debug(
                    f"Refreshing Spotify access token (attempt {attempt}/{max_retries})..."
                )
                token_info = auth_manager.refresh_access_token(refresh_token)
                log.info("✅ Obtained new Spotify access token.")
                return Spotify(auth=token_info["access_token"])

            except (SpotifyOauthError, requests.exceptions.RequestException) as e:
                log.warning(
                    f"Spotify token refresh failed "
                    f"(attempt {attempt}/{max_retries}): {e}"
                )

                if attempt < max_retries:
                    _sleep_backoff(attempt, base_seconds=base_backoff_seconds)
                    continue

                log.error("❌ Exceeded maximum retries while refreshing Spotify token.")
                raise

            except Exception as e:
                log.error(
                    f"❌ Unexpected error while refreshing Spotify token: {e}",
                    exc_info=True,
                )
                raise

    def _call_with_retry(self, fn, *, context: str, max_retries: int = 3):
        for attempt in range(1, max_retries + 1):
            try:
                return fn()
            except requests.exceptions.ReadTimeout as e:
                log.warning(
                    f"Spotify timeout while {context} (attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    _sleep_backoff(attempt)
                    continue
                raise
            except SpotifyException as e:
                if e.http_status == 429:
                    log.warning(
                        f"Rate limited by Spotify API while {context}. Respecting Retry-After."
                    )
                    _sleep_for_rate_limit(e)
                    continue
                if _is_retryable_spotify_exception(e) and attempt < max_retries:
                    log.warning(
                        f"Retryable Spotify error while {context} (attempt {attempt}/{max_retries}): {e}"
                    )
                    _sleep_backoff(attempt)
                    continue
                raise

    # --- Public facade methods ---

    def search_track(self, artist: str, title: str) -> str | None:
        sp = self.client
        query = f"artist:{artist} track:{title}"

        try:
            results = self._call_with_retry(
                lambda: sp.search(q=query, type="track", limit=1),
                context=f"searching track '{artist} - {title}'",
            )
        except Exception as e:
            log.error(f"Unexpected error during Spotify search: {e}", exc_info=True)
            return None

        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            log.warning(
                f"No track found for the given artist/title: {artist} - {title}"
            )
            return None

        found_artist = (
            tracks[0]["artists"][0]["name"]
            if tracks[0].get("artists")
            else "Unknown Artist"
        )
        found_title = tracks[0].get("name", "Unknown Title")
        string_original_track = f"{artist} - {title}"
        string_found_track = f"{found_artist} - {found_title}"

        if string_original_track.lower() != string_found_track.lower():
            log.warning(
                f"Original track: {string_original_track} (URI: {tracks[0]['uri']})"
            )
            log.warning(f"Found track: {string_found_track} (URI: {tracks[0]['uri']})")
        else:
            log.info(f"Found track: {string_found_track} (URI: {tracks[0]['uri']})")

        return tracks[0]["uri"]

    def create_playlist(self, name: str, description: str) -> str | None:
        sp = self.client
        try:
            user_id = self._call_with_retry(
                lambda: sp.current_user()["id"], context="getting current user"
            )
            playlist = self._call_with_retry(
                lambda: sp.user_playlist_create(
                    user=user_id, name=name, public=True, description=description
                ),
                context=f"creating playlist '{name}'",
            )
            playlist_id = playlist["id"]
            log.info(f"✅ Created Spotify playlist '{name}' (ID: {playlist_id})")
            return playlist_id
        except Exception as e:
            log.error(f"❌ Failed to create playlist '{name}': {e}")
            return None

    def add_tracks_to_specific_playlist(
        self, playlist_id: str, uris: list[str], allowDuplicates: bool = False
    ) -> None:
        if not playlist_id:
            raise ValueError("Missing playlist_id parameter.")
        if not uris:
            log.warning("No tracks to add.")
            return

        unique_uris = list(dict.fromkeys(uris))
        if len(unique_uris) != len(uris):
            log.info(
                f"Removed {len(uris) - len(unique_uris)} duplicate track(s) from input list."
            )

        sp = self.client

        if allowDuplicates:
            uris_to_add = unique_uris
        else:
            existing_uris = set()
            offset = 0
            while True:
                resp = self._call_with_retry(
                    lambda offset=offset: sp.playlist_items(
                        playlist_id,
                        fields="items.track.uri,total,next",
                        additional_types=["track"],
                        limit=100,
                        offset=offset,
                    ),
                    context=f"fetching playlist items for {playlist_id}",
                )
                for item in resp.get("items", []) or []:
                    track = item.get("track")
                    if track and "uri" in track:
                        existing_uris.add(track["uri"])
                if not resp.get("next"):
                    break
                offset += 100

            uris_to_add = [uri for uri in unique_uris if uri not in existing_uris]
            skipped = len(unique_uris) - len(uris_to_add)
            if skipped > 0:
                log.info(f"Skipped {skipped} track(s) already present in the playlist.")

        if not uris_to_add:
            log.info(
                "No new tracks to add after filtering existing tracks."
                if not allowDuplicates
                else "No tracks to add."
            )
            return

        self._call_with_retry(
            lambda: sp.playlist_add_items(playlist_id, uris_to_add),
            context=f"adding {len(uris_to_add)} tracks to playlist {playlist_id}",
        )
        log.info(f"Added {len(uris_to_add)} track(s) to playlist {playlist_id}.")

    def get_playlist_tracks(self, playlist_id: str) -> list[str]:
        if not playlist_id:
            return []

        sp = self.client
        tracks: list[str] = []
        offset = 0

        try:
            while True:
                response = self._call_with_retry(
                    lambda offset=offset: sp.playlist_items(
                        playlist_id,
                        fields="items.track.uri,total,next",
                        additional_types=["track"],
                        limit=100,
                        offset=offset,
                    ),
                    context=f"fetching playlist tracks for {playlist_id}",
                )
                items = response.get("items") or []
                for item in items:
                    track = item.get("track")
                    if track and "uri" in track:
                        tracks.append(track["uri"])
                if not response.get("next"):
                    break
                offset += 100
            return tracks
        except Exception as e:
            log.error(
                f"❌ Failed to retrieve playlist tracks for {playlist_id}: {e}",
                exc_info=True,
            )
            return []

    def clear_playlist(self, playlist_id: str) -> None:
        """Remove all tracks from a playlist, leaving it empty.

        Fetches current tracks via get_playlist_tracks, then removes them
        in batches of 100 using playlist_remove_all_occurrences_of_items.
        No-ops silently if the playlist is already empty.
        Never raises — logs errors and returns.
        """
        try:
            uris = self.get_playlist_tracks(playlist_id)
            if not uris:
                log.info(f"Playlist {playlist_id} is already empty")
                return

            sp = self.client
            for i in range(0, len(uris), 100):
                batch = uris[i : i + 100]
                self._call_with_retry(
                    lambda batch=batch: sp.playlist_remove_all_occurrences_of_items(
                        playlist_id, batch
                    ),
                    context=f"clearing playlist {playlist_id} (batch of {len(batch)})",
                )
            log.info(f"Cleared {len(uris)} tracks from playlist {playlist_id}")
        except Exception as e:
            log.error(f"❌ Failed to clear playlist {playlist_id}: {e}")

    def find_playlist_by_name(self, name: str):
        try:
            sp = (
                self._client_from_refresh()
                if config.SPOTIPY_REFRESH_TOKEN
                else self.client
            )
            results = self._call_with_retry(
                lambda: sp.current_user_playlists(limit=50), context="listing playlists"
            )

            for playlist in results.get("items", []) or []:
                if playlist.get("name") == name:
                    log.info(
                        f"✅ Match found: {playlist.get('name')} (ID={playlist.get('id')})"
                    )
                    return {"id": playlist["id"], "data": playlist}

            log.warning(f"⚠️ No playlist found with name '{name}'")
            return None
        except Exception as e:
            log.error(
                f"❌ Exception while searching for playlist '{name}': {e}",
                exc_info=True,
            )
            return None

    def trim_playlist_to_limit(self, limit: int = 200) -> None:
        if not config.SPOTIFY_PLAYLIST_ID:
            raise OSError("Missing SPOTIFY_PLAYLIST_ID environment variable.")

        sp = self.client
        current = self._call_with_retry(
            lambda: sp.playlist_items(
                config.SPOTIFY_PLAYLIST_ID,
                fields="items.track.uri,total",
                additional_types=["track"],
            ),
            context="fetching current playlist items",
        )
        total = current["total"]
        if total <= limit:
            log.info(f"Playlist is within limit ({total}/{limit}); no tracks removed.")
            return

        num_to_remove = total - limit
        uris_to_remove = [
            item["track"]["uri"] for item in current["items"][:num_to_remove]
        ]
        self._call_with_retry(
            lambda: sp.playlist_remove_all_occurrences_of_items(
                config.SPOTIFY_PLAYLIST_ID, uris_to_remove
            ),
            context=f"removing {num_to_remove} tracks",
        )
        log.info(f"Removed {len(uris_to_remove)} old tracks to stay under {limit}.")


def get_spotify_client() -> Spotify:
    """Return Spotify client, preferring refresh-token flow in CI."""
    global _spotify_api
    if _spotify_api is None:
        _spotify_api = SpotifyAPI.from_env()
    return _spotify_api.client


def get_spotify_client_from_refresh() -> Spotify:
    global _spotify_api
    if _spotify_api is None:
        _spotify_api = SpotifyAPI.from_env()
    return _spotify_api._client_from_refresh()


# --- Singleton accessor for SpotifyAPI ---


def _get_api() -> SpotifyAPI:
    global _spotify_api
    if _spotify_api is None:
        _spotify_api = SpotifyAPI.from_env()
    return _spotify_api


def search_track(artist: str, title: str) -> str | None:
    return _get_api().search_track(artist, title)


def trim_playlist_to_limit(limit: int = 200) -> None:
    _get_api().trim_playlist_to_limit(limit)


def create_playlist(
    name: str,
    description: str = "Generated automatically by Deejay Marvel Automation Tools. Spreadsheets of history, and song not found on Spotify can be found at www.kaianolevine.com/dj-marvel",
) -> str | None:
    return _get_api().create_playlist(name, description)


def add_tracks_to_playlist(uris: list[str], allowDuplicates: bool = False) -> None:
    _get_api().add_tracks_to_specific_playlist(
        config.SPOTIFY_PLAYLIST_ID, uris, allowDuplicates=allowDuplicates
    )


def add_tracks_to_specific_playlist(
    playlist_id: str, uris: list[str], allowDuplicates: bool = False
) -> None:
    _get_api().add_tracks_to_specific_playlist(
        playlist_id, uris, allowDuplicates=allowDuplicates
    )


def find_playlist_by_name(name: str):
    return _get_api().find_playlist_by_name(name)


def get_playlist_tracks(playlist_id: str) -> list[str]:
    return _get_api().get_playlist_tracks(playlist_id)


def clear_playlist(playlist_id: str) -> None:
    _get_api().clear_playlist(playlist_id)
