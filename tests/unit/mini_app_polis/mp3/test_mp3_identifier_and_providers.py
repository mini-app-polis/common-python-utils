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

    from mini_app_polis.mp3.identify.providers import acoustid_provider

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

    from mini_app_polis.mp3.identify.providers import acoustid_provider

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
    from mini_app_polis.mp3.identify.identifier import TrackId
    from mini_app_polis.mp3.identify.providers.musicbrainz_provider import (
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
    from mini_app_polis.mp3.identify.identifier import TrackId
    from mini_app_polis.mp3.identify.providers.musicbrainz_provider import (
        MusicBrainzRecordingProvider,
    )

    prov = MusicBrainzRecordingProvider(throttle_s=0.0, retries=1)
    with pytest.raises(ValueError):
        prov.fetch(TrackId(provider="acoustid", id="x", confidence=1.0))


def test_mp3_identifier_chooses_highest_confidence_and_fetches_metadata(
    stubbed_optional_deps,
):
    from mini_app_polis.mp3.identify.identifier import (
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
    from mini_app_polis.mp3.identify.identifier import (
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
    acoustid_mod = types.ModuleType(
        "mini_app_polis.mp3.identify.providers.acoustid_provider"
    )

    class AcoustIdIdentifier:
        def __init__(self, api_key, min_confidence, max_candidates):
            self.api_key = api_key
            self.min_confidence = min_confidence
            self.max_candidates = max_candidates

        def identify(self, _path):
            return []

    acoustid_mod.AcoustIdIdentifier = AcoustIdIdentifier  # type: ignore[attr-defined]
    sys.modules["mini_app_polis.mp3.identify.providers.acoustid_provider"] = (
        acoustid_mod
    )

    mb_mod = types.ModuleType(
        "mini_app_polis.mp3.identify.providers.musicbrainz_provider"
    )

    class MusicBrainzRecordingProvider:
        def __init__(self, app_name, app_version, contact, throttle_s):
            self.args = (app_name, app_version, contact, throttle_s)

        def fetch(self, _track_id):
            return {"ok": True}

    mb_mod.MusicBrainzRecordingProvider = MusicBrainzRecordingProvider  # type: ignore[attr-defined]
    sys.modules["mini_app_polis.mp3.identify.providers.musicbrainz_provider"] = mb_mod

    ident_mod = importlib.reload(
        importlib.import_module("mini_app_polis.mp3.identify.identifier")
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


# ---------------------------------------------------------------------------
# Provider behaviour with the third-party module and ``time`` patched onto the
# provider itself, so nothing depends on which stub another test installed.
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_module(monkeypatch):
    """Import a provider module fresh.

    ``test_mp3_identifier_from_env_wires_providers`` swaps stand-ins for the
    provider modules into ``sys.modules``; this sets them aside for the test.
    """
    import importlib

    def _load(name: str):
        full = f"mini_app_polis.mp3.identify.providers.{name}"
        monkeypatch.delitem(sys.modules, full, raising=False)
        return importlib.import_module(full)

    return _load


class _FakeTime:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def _musicbrainz(provider_module, monkeypatch, get_recording_by_id, **kwargs):
    from unittest.mock import MagicMock

    mod = provider_module("musicbrainz_provider")
    mb = MagicMock()
    mb.get_recording_by_id.side_effect = get_recording_by_id
    fake_time = _FakeTime()
    monkeypatch.setattr(mod, "musicbrainzngs", mb)
    monkeypatch.setattr(mod, "time", fake_time)
    kwargs.setdefault("throttle_s", 0.0)
    return mod.MusicBrainzRecordingProvider(**kwargs), mb, fake_time


def _mbid(id_: str = "mbid-1"):
    from mini_app_polis.mp3.identify.identifier import TrackId

    return TrackId(provider="musicbrainz", id=id_, confidence=1.0)


def test_musicbrainz_sets_user_agent_and_requests_the_needed_includes(
    provider_module, monkeypatch
):
    prov, mb, _ = _musicbrainz(
        provider_module,
        monkeypatch,
        lambda *_a, **_k: {"recording": {"title": "T"}},
        app_name="app",
        app_version="2.0",
        contact="me@example.com",
    )

    meta = prov.fetch(_mbid("abc"))

    mb.set_useragent.assert_called_once_with("app", "2.0", "me@example.com")
    mb.get_recording_by_id.assert_called_once_with(
        "abc", includes=["artists", "releases", "isrcs", "tags"]
    )
    assert meta == {
        "title": "T",
        "artist": None,
        "album": None,
        "year": None,
        "isrc": None,
        "genre": None,
        "raw": {"musicbrainz_recording": {"title": "T"}},
        "_provider": "musicbrainz",
        "_mbid": "abc",
    }


def test_musicbrainz_throttles_calls_closer_together_than_throttle_s(
    provider_module, monkeypatch
):
    prov, _, fake_time = _musicbrainz(
        provider_module,
        monkeypatch,
        lambda *_a, **_k: {"recording": {}},
        throttle_s=1.0,
    )

    prov.fetch(_mbid())  # first call: last call was at epoch 0
    fake_time.now += 0.25
    prov.fetch(_mbid())

    assert fake_time.sleeps == [0.75]


def test_musicbrainz_retries_with_linear_backoff_then_succeeds(
    provider_module, monkeypatch
):
    outcomes = [RuntimeError("503"), RuntimeError("503"), {"recording": {"title": "T"}}]

    def get(*_a, **_k):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    prov, mb, fake_time = _musicbrainz(
        provider_module, monkeypatch, get, retries=3, retry_sleep_s=0.5
    )

    assert prov.fetch(_mbid())["title"] == "T"
    assert mb.get_recording_by_id.call_count == 3
    assert fake_time.sleeps == [0.5, 1.0]


def test_musicbrainz_raises_after_the_last_retry_chaining_the_cause(
    provider_module, monkeypatch
):
    boom = RuntimeError("network down")

    def get(*_a, **_k):
        raise boom

    prov, mb, fake_time = _musicbrainz(
        provider_module, monkeypatch, get, retries=2, retry_sleep_s=1.0
    )

    with pytest.raises(RuntimeError, match="MusicBrainz fetch failed for mbid-1") as ei:
        prov.fetch(_mbid())
    assert ei.value.__cause__ is boom
    assert mb.get_recording_by_id.call_count == 2
    assert fake_time.sleeps == [1.0]


def test_musicbrainz_non_dict_response_yields_empty_metadata(
    provider_module, monkeypatch
):
    prov, _, _ = _musicbrainz(provider_module, monkeypatch, lambda *_a, **_k: None)

    meta = prov.fetch(_mbid())

    assert meta["title"] is None
    assert meta["raw"] == {"musicbrainz_recording": {}}


@pytest.mark.parametrize(
    ("recording", "expected"),
    [
        # artist credit: nested artist name, else the credit's own name
        ({"artist-credit": [{"name": "Credit"}]}, {"artist": "Credit"}),
        ({"artist-credit": ["joinphrase"]}, {"artist": None}),
        ({"artist-credit": [{"artist": "not-a-dict"}]}, {"artist": None}),
        # release: short or missing dates give no year
        (
            {"release-list": [{"title": "Alb", "date": "19"}]},
            {"album": "Alb", "year": None},
        ),
        ({"release-list": [{"title": "Alb"}]}, {"album": "Alb", "year": None}),
        ({"release-list": ["junk"]}, {"album": None}),
        ({"release-list": {"not": "a list"}}, {"album": None}),
        # isrc
        ({"isrc-list": [12345]}, {"isrc": "12345"}),
        ({"isrc-list": 7}, {"isrc": None}),
        # genre: highest count wins; unnamed tags are skipped
        (
            {
                "tag-list": [
                    {"name": "pop", "count": "2"},
                    {"name": "", "count": "9"},
                    {"name": "wcs", "count": "5"},
                ]
            },
            {"genre": "wcs"},
        ),
        ({"tag-list": [{"name": "b", "count": "x"}, {"name": "a"}]}, {"genre": "b"}),
        ({"tag-list": [{"count": "1"}]}, {"genre": None}),
        ({"tag-list": ["not-a-dict"]}, {"genre": None}),
    ],
)
def test_musicbrainz_a_malformed_field_degrades_to_none_without_losing_others(
    provider_module, monkeypatch, recording, expected
):
    payload = {"title": "Kept", **recording}
    prov, _, _ = _musicbrainz(
        provider_module, monkeypatch, lambda *_a, **_k: {"recording": payload}
    )

    meta = prov.fetch(_mbid())

    assert meta["title"] == "Kept"
    for key, value in expected.items():
        assert meta[key] == value, key


def _acoustid(provider_module, monkeypatch, *, match, lookup=None, run=None):
    from unittest.mock import MagicMock

    mod = provider_module("acoustid_provider")
    ac = MagicMock()
    ac.match.side_effect = match
    ac.lookup.side_effect = lookup or (lambda *_a, **_k: {"results": []})
    fake_time = _FakeTime()
    runner = MagicMock(
        side_effect=run
        or (lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout=""))
    )
    monkeypatch.setattr(mod, "acoustid", ac)
    monkeypatch.setattr(mod, "time", fake_time)
    monkeypatch.setattr(mod.subprocess, "run", runner)
    monkeypatch.setattr(mod, "log", MagicMock())
    return mod, ac, fake_time, runner


def _decode_error(*_a, **_k):
    raise RuntimeError("decode")


def _fpcalc(stdout: str, returncode: int = 0):
    return lambda *_a, **_k: types.SimpleNamespace(returncode=returncode, stdout=stdout)


def test_acoustid_treats_a_non_numeric_score_as_zero(
    provider_module, monkeypatch, tmp_path
):
    mod, ac, _, _ = _acoustid(
        provider_module,
        monkeypatch,
        match=lambda *_a: iter(
            [("n/a", "mbid-x", "T", "A"), (0.95, "mbid-ok", "T", "A")]
        ),
    )

    out = mod.AcoustIdIdentifier(api_key="key").identify(str(tmp_path / "a.mp3"))

    assert [(c.id, c.confidence) for c in out] == [("mbid-ok", 0.95)]
    ac.match.assert_called_once_with("key", str(tmp_path / "a.mp3"))


def test_acoustid_retries_when_match_and_fpcalc_both_fail(
    provider_module, monkeypatch, tmp_path
):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"\x00\x01")
    mod, ac, fake_time, runner = _acoustid(
        provider_module, monkeypatch, match=_decode_error
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=3, retry_sleep_s=0.5).identify(
        str(f)
    )

    assert out == []
    assert ac.match.call_count == 3
    assert fake_time.sleeps == [0.5, 1.0]
    runner.assert_called_with(
        ["fpcalc", "-json", str(f)], check=False, capture_output=True, text=True
    )
    ac.lookup.assert_not_called()
    errors = " ".join(c.args[0] for c in mod.log.error.call_args_list)
    assert "size_bytes=2 head32_hex=0001" in errors


def test_acoustid_survives_a_missing_file_and_a_missing_fpcalc(
    provider_module, monkeypatch, tmp_path
):
    def no_fpcalc(*_a, **_k):
        raise FileNotFoundError("fpcalc")

    mod, _, _, _ = _acoustid(
        provider_module, monkeypatch, match=_decode_error, run=no_fpcalc
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=1).identify(
        str(tmp_path / "missing.mp3")
    )

    assert out == []
    errors = " ".join(c.args[0] for c in mod.log.error.call_args_list)
    assert "dbg failed" in errors
    assert "ACOUSTID-FALLBACK-ERROR" in errors


def test_acoustid_fallback_extracts_json_from_noisy_fpcalc_output(
    provider_module, monkeypatch, tmp_path
):
    mod, ac, _, _ = _acoustid(
        provider_module,
        monkeypatch,
        match=_decode_error,
        run=_fpcalc('WARNING: junk\n{"duration": 181.5, "fingerprint": "FP"}\ntrailer'),
        lookup=lambda *_a, **_k: {
            "results": [
                {"score": 0.5, "recordings": [{"id": "too-low"}]},
                {"score": 0.92, "recordings": [{"id": "r-92"}, {"title": "no id"}]},
                {"score": 0.99, "recordings": [{"id": "r-99"}]},
                {"score": None},
            ]
        },
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=1, max_candidates=5).identify(
        str(tmp_path / "a.mp3")
    )

    assert [(c.id, c.confidence) for c in out] == [("r-99", 0.99), ("r-92", 0.92)]
    ac.lookup.assert_called_once_with(
        "k", "FP", 181.5, meta="recordings+releasegroups+compress"
    )


def test_acoustid_fallback_caps_candidates(provider_module, monkeypatch, tmp_path):
    mod, _, _, _ = _acoustid(
        provider_module,
        monkeypatch,
        match=_decode_error,
        run=_fpcalc('{"duration": 10, "fingerprint": "FP"}'),
        lookup=lambda *_a, **_k: {
            "results": [
                {"score": 0.95, "recordings": [{"id": f"r{i}"} for i in range(4)]}
            ]
        },
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=1, max_candidates=2).identify(
        str(tmp_path / "a.mp3")
    )

    assert [c.id for c in out] == ["r0", "r1"]


@pytest.mark.parametrize(
    "stdout",
    ["not json at all", '{"duration": 10}', '{"fingerprint": "FP", "duration": 0}'],
    ids=["unparseable", "no-fingerprint", "zero-duration"],
)
def test_acoustid_fallback_skips_lookup_without_a_usable_fingerprint(
    provider_module, monkeypatch, tmp_path, stdout
):
    mod, ac, _, _ = _acoustid(
        provider_module, monkeypatch, match=_decode_error, run=_fpcalc(stdout)
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=1).identify(
        str(tmp_path / "a.mp3")
    )

    assert out == []
    ac.lookup.assert_not_called()


def test_acoustid_fallback_lookup_failure_falls_through_to_retry(
    provider_module, monkeypatch, tmp_path
):
    def lookup_fails(*_a, **_k):
        raise RuntimeError("acoustid 503")

    mod, ac, fake_time, _ = _acoustid(
        provider_module,
        monkeypatch,
        match=_decode_error,
        run=_fpcalc('{"duration": 10, "fingerprint": "FP"}'),
        lookup=lookup_fails,
    )

    out = mod.AcoustIdIdentifier(api_key="k", retries=2, retry_sleep_s=1.0).identify(
        str(tmp_path / "a.mp3")
    )

    assert out == []
    assert ac.lookup.call_count == 2
    assert fake_time.sleeps == [1.0]
    errors = " ".join(c.args[0] for c in mod.log.error.call_args_list)
    assert "ACOUSTID-FALLBACK-LOOKUP-ERROR" in errors


def test_acoustid_fallback_ignores_a_non_dict_lookup_response(
    provider_module, monkeypatch, tmp_path
):
    mod, _, _, _ = _acoustid(
        provider_module,
        monkeypatch,
        match=_decode_error,
        run=_fpcalc('{"duration": 10, "fingerprint": "FP"}'),
        lookup=lambda *_a, **_k: ["unexpected"],
    )

    assert (
        mod.AcoustIdIdentifier(api_key="k", retries=1).identify(str(tmp_path / "a.mp3"))
        == []
    )
