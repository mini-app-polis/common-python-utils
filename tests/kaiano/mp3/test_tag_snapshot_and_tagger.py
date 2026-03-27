import importlib
import sys
import types

from kaiano.mp3.tag.tagger import Mp3Tagger


def _install(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


def test_tag_snapshot_reader_joins_lists_and_ignores_errors(tmp_path, monkeypatch):
    # Create a fake music_tag module before importing the snapshot reader.
    music_tag = types.ModuleType("music_tag")

    class FakeFile:
        def __contains__(self, key):
            return key in {
                "tracktitle",
                "artist",
                "genre",
                "bpm",
            }

        def __getitem__(self, key):
            if key == "tracktitle":
                return "Song"
            if key == "artist":
                return ["A1", None, "A2"]
            if key == "genre":
                raise RuntimeError("boom")
            if key == "bpm":
                return None
            raise KeyError(key)

    music_tag.load_file = lambda _path: FakeFile()  # type: ignore[attr-defined]
    _install("music_tag", music_tag)

    # Reload to pick up our stub.
    mod = importlib.reload(
        importlib.import_module("kaiano.mp3.identify.io.tag_snapshot")
    )

    r = mod.MusicTagSnapshotReader()
    f = tmp_path / "x.mp3"
    f.write_bytes(b"ID3" + b"0" * 10)

    out = r.read(str(f))
    assert out["tracktitle"] == "Song"
    assert out["artist"] == "A1, A2"
    # bpm stored as empty string when None
    assert out["bpm"] == ""
    # genre error is ignored
    assert "genre" not in out


def test_music_tag_io_read_write_and_dump(tmp_path, monkeypatch):
    # Stub kaiano.logger (google conftest already installs one, but keep simple)
    # Stub music_tag
    music_tag = types.ModuleType("music_tag")

    class FakeFile(dict):
        def __init__(self):
            super().__init__()
            self._saved = False
            self["artwork"] = True

        def save(self):
            self._saved = True

        def keys(self):
            return super().keys()

    file_obj = FakeFile()
    music_tag.load_file = lambda _path: file_obj  # type: ignore[attr-defined]
    _install("music_tag", music_tag)

    # Stub mutagen.id3 so VirtualDJ compat path is exercised.
    mutagen_id3 = types.ModuleType("mutagen.id3")

    class ID3NoHeaderError(Exception):
        pass

    class TYER:
        def __init__(self, encoding, text):
            _ = encoding
            self.text = text

    class TDRC:
        def __init__(self, encoding, text):
            _ = encoding
            self.text = text

    class ID3:
        def __init__(self, path=None):
            self.path = path
            self.set_calls = []

        def setall(self, key, frames):
            self.set_calls.append((key, frames[0].text))

        def save(self, path, v2_version=3):
            self.saved = (path, v2_version)

    mutagen_id3.ID3 = ID3  # type: ignore[attr-defined]
    mutagen_id3.TYER = TYER  # type: ignore[attr-defined]
    mutagen_id3.TDRC = TDRC  # type: ignore[attr-defined]
    mutagen_id3.ID3NoHeaderError = ID3NoHeaderError  # type: ignore[attr-defined]
    _install("mutagen.id3", mutagen_id3)

    # Reload module under test.
    io_mod = importlib.reload(importlib.import_module("kaiano.mp3.tag.io.music_tag_io"))
    MusicTagIO = io_mod.MusicTagIO

    f = tmp_path / "song.mp3"
    f.write_bytes(b"ID3" + b"0" * 10)

    io = MusicTagIO()

    # Write tags and ensure save + v2.3 compat
    io.write(
        str(f),
        {
            "title": "Song",
            "artist": "Artist",
            "year": "2020-01-02",
            "bpm": 120,
            "comment": "C",
            "album_artist": "AA",
        },
        ensure_virtualdj_compat=True,
    )
    assert file_obj._saved is True
    assert file_obj["tracktitle"] == "Song"
    assert file_obj["albumartist"] == "AA"
    assert file_obj["year"] == "2020-01-02"

    snap = io.read(str(f))
    assert snap.has_artwork is True
    assert snap.tags.get("tracktitle") == "Song"

    dumped = io.dump_tags(str(f))
    assert dumped["tracktitle"] == "Song"


def test_mp3_tagger_is_thin_wrapper(monkeypatch):
    from kaiano.mp3.tag.models import TagSnapshot

    class IO:
        def __init__(self):
            self.calls = []

        def read(self, path):
            self.calls.append(("read", path))
            return TagSnapshot(tags={"a": "b"}, has_artwork=False)

        def write(self, path, metadata, ensure_virtualdj_compat=False):
            self.calls.append(("write", path, dict(metadata), ensure_virtualdj_compat))

        def dump_tags(self, path):
            self.calls.append(("dump", path))
            return {"x": "y"}

    io = IO()
    t = Mp3Tagger(io=io)
    assert t.read("p").tags == {"a": "b"}
    t.write("p", {"title": "t"}, ensure_virtualdj_compat=True)
    assert t.dump("p") == {"x": "y"}


# ---------------------------------------------------------------------------
# sanitize_string
# ---------------------------------------------------------------------------


def test_sanitize_string_handles_none_and_whitespace():
    assert Mp3Tagger.sanitize_string(None) == ""
    assert Mp3Tagger.sanitize_string("") == ""
    assert Mp3Tagger.sanitize_string("   ") == ""
    assert Mp3Tagger.sanitize_string("  hello  ") == "hello"
    assert Mp3Tagger.sanitize_string("\tworld\n") == "world"
    assert Mp3Tagger.sanitize_string(123) == "123"


def test_sanitize_string_is_idempotent():
    v = "hello"
    assert Mp3Tagger.sanitize_string(v) == Mp3Tagger.sanitize_string(
        Mp3Tagger.sanitize_string(v)
    )


# ---------------------------------------------------------------------------
# build_routine_tag_title
# ---------------------------------------------------------------------------


def test_build_routine_tag_title_leader_and_follower():
    title = Mp3Tagger.build_routine_tag_title(
        leader_first="Alice",
        leader_last="Leader",
        follower_first="Bob",
        follower_last="Follower",
    )
    assert title == "Alice Leader & Bob Follower"


def test_build_routine_tag_title_trims_whitespace():
    title = Mp3Tagger.build_routine_tag_title(
        leader_first="  Alice ",
        leader_last=" Leader ",
        follower_first=" Bob",
        follower_last="Follower  ",
    )
    assert title == "Alice Leader & Bob Follower"


def test_build_routine_tag_title_leader_only():
    title = Mp3Tagger.build_routine_tag_title(
        leader_first="Alice",
        leader_last="Leader",
        follower_first="",
        follower_last="",
    )
    assert title == "Alice Leader"


def test_build_routine_tag_title_follower_only():
    title = Mp3Tagger.build_routine_tag_title(
        leader_first="",
        leader_last="",
        follower_first="Bob",
        follower_last="Follower",
    )
    assert title == "Bob Follower"


def test_build_routine_tag_title_all_empty():
    title = Mp3Tagger.build_routine_tag_title(
        leader_first="",
        leader_last="",
        follower_first="",
        follower_last="",
    )
    assert title == ""


# ---------------------------------------------------------------------------
# build_routine_tag_artist
# ---------------------------------------------------------------------------


def test_build_routine_tag_artist_all_fields_present():
    artist = Mp3Tagger.build_routine_tag_artist(
        version="1",
        division="Novice",
        season_year="2025",
        routine_name="My Routine",
        personal_descriptor="Practice",
    )
    assert artist == "v1 | Novice 2025 | My Routine | Practice"


def test_build_routine_tag_artist_optional_fields_missing():
    artist = Mp3Tagger.build_routine_tag_artist(
        version="2",
        division="Advanced",
        season_year="2026",
        routine_name="",
        personal_descriptor="",
    )
    assert artist == "v2 | Advanced 2026"


def test_build_routine_tag_artist_trims_and_sanitizes():
    artist = Mp3Tagger.build_routine_tag_artist(
        version=" 3 ",
        division=" Advanced ",
        season_year=" 2027 ",
        routine_name="  Showcase ",
        personal_descriptor="  Finals ",
    )
    assert artist == "v3 | Advanced 2027 | Showcase | Finals"


def test_build_routine_tag_artist_never_returns_empty_string_when_base_present():
    artist = Mp3Tagger.build_routine_tag_artist(
        version="1",
        division="Open",
        season_year="2024",
        routine_name="",
        personal_descriptor="",
    )
    assert artist.startswith("v1 | Open 2024")
