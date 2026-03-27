import sys
import types

import pytest


def _install(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


@pytest.fixture
def stubbed_optional_deps(monkeypatch):
    """Provide stubs for optional third-party deps used by mp3 modules."""

    # acoustid stub
    acoustid = types.ModuleType("acoustid")

    def _match(api_key, path):
        # default: no results
        return []

    def _lookup(api_key, fingerprint, duration, meta=None):
        return {"results": []}

    acoustid.match = _match  # type: ignore[attr-defined]
    acoustid.lookup = _lookup  # type: ignore[attr-defined]
    _install("acoustid", acoustid)

    # musicbrainzngs stub
    musicbrainzngs = types.ModuleType("musicbrainzngs")
    musicbrainzngs._ua = None

    def set_useragent(app_name, app_version, contact):
        musicbrainzngs._ua = (app_name, app_version, contact)

    def get_recording_by_id(_id, includes=None):
        return {"recording": {"title": "T", "artist-credit": [{"name": "A"}]}}

    musicbrainzngs.set_useragent = set_useragent  # type: ignore[attr-defined]
    musicbrainzngs.get_recording_by_id = get_recording_by_id  # type: ignore[attr-defined]
    _install("musicbrainzngs", musicbrainzngs)

    yield


def test_acoustid_provider_filters_and_sorts(
    tmp_path, stubbed_optional_deps, monkeypatch
):
    import importlib

    from kaiano.mp3.identify.providers import acoustid_provider

    # Arrange
    f = tmp_path / "song.mp3"
    f.write_bytes(b"ID3" + b"0" * 100)

    def match(_api_key, _path):
        return [
            ("0.50", "mbid-low", "Title", "Artist"),
            ("0.95", "mbid-hi", "Title", "Artist"),
            (0.97, "mbid-top", "Title", "Artist"),
            (0.99, "", "Title", "Artist"),  # missing id ignored
        ]

    sys.modules["acoustid"].match = match  # type: ignore[attr-defined]
    # Reload so the provider binds to the latest stub module.
    acoustid_provider = importlib.reload(acoustid_provider)

    ident = acoustid_provider.AcoustIdIdentifier(
        api_key="k", min_confidence=0.9, max_candidates=1
    )

    # Act
    candidates = list(ident.identify(str(f)))

    # Assert: only highest confidence, id present, above threshold
    assert len(candidates) == 1
    assert candidates[0].id == "mbid-top"
    assert candidates[0].provider == "musicbrainz"


def test_acoustid_provider_fallback_fpcalc_lookup(
    tmp_path, stubbed_optional_deps, monkeypatch
):
    import importlib

    from kaiano.mp3.identify.providers import acoustid_provider

    f = tmp_path / "bad.mp3"
    f.write_bytes(b"ID3" + b"X" * 64)

    # Force acoustid.match to error so fallback triggers.
    def boom(*_a, **_k):
        raise RuntimeError("decode")

    sys.modules["acoustid"].match = boom  # type: ignore[attr-defined]

    # fpcalc returns JSON with duration+fingerprint
    class _P:
        returncode = 0
        stdout = '{"duration": 10.0, "fingerprint": "abc"}'

    # Reload so the provider binds to the latest stub module.
    acoustid_provider = importlib.reload(acoustid_provider)

    monkeypatch.setattr(acoustid_provider.subprocess, "run", lambda *_a, **_k: _P())

    # lookup returns a result with recordings
    def lookup(_api_key, _fp, _dur, meta=None):
        return {
            "results": [
                {
                    "score": 0.93,
                    "recordings": [{"id": "rid-1"}, {"id": "rid-2"}],
                }
            ]
        }

    sys.modules["acoustid"].lookup = lookup  # type: ignore[attr-defined]

    ident = acoustid_provider.AcoustIdIdentifier(
        api_key="k", min_confidence=0.9, max_candidates=5, retries=1
    )
    out = list(ident.identify(str(f)))

    assert [c.id for c in out] == ["rid-1", "rid-2"]


def test_musicbrainz_provider_fetch_happy_path(stubbed_optional_deps, monkeypatch):
    from kaiano.mp3.identify.identifier import TrackId
    from kaiano.mp3.identify.providers.musicbrainz_provider import (
        MusicBrainzRecordingProvider,
    )

    # Make time deterministic to avoid sleeps in throttle.
    monkeypatch.setattr("time.time", lambda: 100.0)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    # Provide richer payload.
    def get_recording_by_id(_id, includes=None):
        return {
            "recording": {
                "title": "Song",
                "artist-credit": [{"artist": {"name": "Artist"}}],
                "release-list": [{"title": "Album", "date": "2020-01-02"}],
                "isrc-list": ["ISRC1"],
                "tag-list": [{"name": "house", "count": "10"}],
            }
        }

    sys.modules["musicbrainzngs"].get_recording_by_id = get_recording_by_id  # type: ignore[attr-defined]

    prov = MusicBrainzRecordingProvider(throttle_s=0.0, retries=1)
    meta = prov.fetch(TrackId(provider="musicbrainz", id="mbid", confidence=1.0))

    assert meta["title"] == "Song"
    assert meta["artist"] == "Artist"
    assert meta["album"] == "Album"
    assert meta["year"] == "2020"
    assert meta["genre"] == "house"
    assert meta["_provider"] == "musicbrainz"


def test_musicbrainz_provider_rejects_non_mbids(stubbed_optional_deps):
    from kaiano.mp3.identify.identifier import TrackId
    from kaiano.mp3.identify.providers.musicbrainz_provider import (
        MusicBrainzRecordingProvider,
    )

    prov = MusicBrainzRecordingProvider(throttle_s=0.0, retries=1)
    with pytest.raises(ValueError):
        prov.fetch(TrackId(provider="acoustid", id="x", confidence=1.0))


def test_mp3_identifier_chooses_highest_confidence_and_fetches_metadata(
    stubbed_optional_deps,
):
    from kaiano.mp3.identify.identifier import (
        IdentificationPolicy,
        Mp3Identifier,
        TrackId,
    )

    class _Acoust:
        def identify(self, _path):
            return [
                TrackId(provider="musicbrainz", id="a", confidence=0.90),
                TrackId(provider="musicbrainz", id="b", confidence=0.95),
            ]

    class _MB:
        def fetch(self, track_id):
            return {"title": "X", "_mbid": track_id.id}

    class _Snap:
        def read(self, _path):
            return {"artist": "A"}

    ident = Mp3Identifier(
        acoustid_identifier=_Acoust(),
        musicbrainz_provider=_MB(),
        policy=IdentificationPolicy(fetch_metadata_min_confidence=0.91),
        snapshot_reader=_Snap(),
    )

    res = ident.identify("/tmp/fake.mp3", fetch_metadata=True)
    assert res.chosen and res.chosen.id == "b"
    assert res.metadata == {"title": "X", "_mbid": "b"}
    assert res.snapshot == {"artist": "A"}


def test_mp3_identifier_skips_metadata_when_confidence_too_low(stubbed_optional_deps):
    from kaiano.mp3.identify.identifier import (
        IdentificationPolicy,
        Mp3Identifier,
        TrackId,
    )

    class _Acoust:
        def identify(self, _path):
            return [TrackId(provider="musicbrainz", id="a", confidence=0.50)]

    class _MB:
        def fetch(self, _track_id):  # pragma: no cover
            raise AssertionError("should not fetch")

    ident = Mp3Identifier(
        acoustid_identifier=_Acoust(),
        musicbrainz_provider=_MB(),
        policy=IdentificationPolicy(fetch_metadata_min_confidence=0.9),
    )

    res = ident.identify("/tmp/fake.mp3", fetch_metadata=True)
    assert res.chosen and res.chosen.id == "a"
    assert res.metadata is None


def test_mp3_identifier_from_env_wires_providers(monkeypatch, stubbed_optional_deps):
    import importlib
    import sys
    import types

    # Provide stub classes for the provider modules imported lazily by from_env.
    acoustid_mod = types.ModuleType("kaiano.mp3.identify.providers.acoustid_provider")

    class AcoustIdIdentifier:
        def __init__(self, api_key, min_confidence, max_candidates):
            self.api_key = api_key
            self.min_confidence = min_confidence
            self.max_candidates = max_candidates

        def identify(self, _path):
            return []

    acoustid_mod.AcoustIdIdentifier = AcoustIdIdentifier  # type: ignore[attr-defined]
    sys.modules["kaiano.mp3.identify.providers.acoustid_provider"] = acoustid_mod

    mb_mod = types.ModuleType("kaiano.mp3.identify.providers.musicbrainz_provider")

    class MusicBrainzRecordingProvider:
        def __init__(self, app_name, app_version, contact, throttle_s):
            self.args = (app_name, app_version, contact, throttle_s)

        def fetch(self, _track_id):
            return {"ok": True}

    mb_mod.MusicBrainzRecordingProvider = MusicBrainzRecordingProvider  # type: ignore[attr-defined]
    sys.modules["kaiano.mp3.identify.providers.musicbrainz_provider"] = mb_mod

    ident_mod = importlib.reload(
        importlib.import_module("kaiano.mp3.identify.identifier")
    )

    ident = ident_mod.Mp3Identifier.from_env(
        acoustid_api_key="k",
        enable_tag_snapshot=False,
        app_name="app",
        app_version="1",
        contact="c",
        throttle_s=0.0,
    )

    assert isinstance(ident._acoustid, AcoustIdIdentifier)
    assert isinstance(ident._mb, MusicBrainzRecordingProvider)
