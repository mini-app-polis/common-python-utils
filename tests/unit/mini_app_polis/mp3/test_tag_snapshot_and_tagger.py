import importlib
import sys
import types

import pytest

from mini_app_polis.mp3.tag.tagger import Mp3Tagger


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
        importlib.import_module("mini_app_polis.mp3.identify.io.tag_snapshot")
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
    # Stub mini_app_polis.logger (google conftest already installs one, but keep simple)
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
    io_mod = importlib.reload(
        importlib.import_module("mini_app_polis.mp3.tag.io.music_tag_io")
    )
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
    from mini_app_polis.mp3.tag.models import TagSnapshot

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


# ---------------------------------------------------------------------------
# MusicTagIO, with music_tag and mutagen patched onto the module itself.
# ---------------------------------------------------------------------------


class _TagFile(dict):
    """A music_tag file: dict access, optional failing keys, tracked saves."""

    def __init__(self, values=None, *, bad_reads=(), bad_writes=(), bad_iter=False):
        super().__init__(values or {})
        self.bad_reads = set(bad_reads)
        self.bad_writes = set(bad_writes)
        self.bad_iter = bad_iter
        self.saves = 0

    def __getitem__(self, key):
        if key in self.bad_reads:
            raise RuntimeError(f"cannot read {key}")
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key in self.bad_writes:
            raise ValueError(f"cannot write {key}")
        super().__setitem__(key, value)

    def __iter__(self):
        if self.bad_iter:
            raise RuntimeError("cannot iterate")
        return super().__iter__()

    def save(self):
        self.saves += 1


class _Frame:
    def __init__(self, encoding, text):
        self.encoding = encoding
        self.text = text


class _ID3:
    """Records what the VirtualDJ compat pass does to the ID3 tag."""

    instances: list["_ID3"] = []
    fail_on_load = False
    fail_on_save = False

    def __init__(self, path=None):
        if path is not None and type(self).fail_on_load:
            raise RuntimeError("no ID3 header")
        self.path = path
        self.frames: dict[str, list] = {}
        self.saved = None
        type(self).instances.append(self)

    def setall(self, key, frames):
        self.frames[key] = [(f.encoding, f.text) for f in frames]

    def save(self, path, v2_version=4):
        if type(self).fail_on_save:
            raise OSError("read-only")
        self.saved = (path, v2_version)


@pytest.fixture
def tag_io(monkeypatch):
    """(module, MusicTagIO(), files-by-path, log) with music_tag/ID3 faked."""
    from unittest.mock import MagicMock

    mod = importlib.import_module("mini_app_polis.mp3.tag.io.music_tag_io")
    files: dict[str, object] = {}

    def load_file(path):
        f = files[path]
        if isinstance(f, Exception):
            raise f
        return f

    id3 = type("ID3", (_ID3,), {"instances": []})
    log = MagicMock()
    monkeypatch.setattr(mod, "music_tag", types.SimpleNamespace(load_file=load_file))
    monkeypatch.setattr(mod, "ID3", id3)
    monkeypatch.setattr(mod, "TYER", _Frame)
    monkeypatch.setattr(mod, "TDRC", _Frame)
    monkeypatch.setattr(mod, "ID3NoHeaderError", type("E", (Exception,), {}))
    monkeypatch.setattr(mod, "log", log)
    return mod, mod.MusicTagIO(), files, log


def test_music_tag_io_read_joins_lists_and_logs_unreadable_keys(tag_io):
    _, io, files, log = tag_io
    files["a.mp3"] = _TagFile(
        {
            "tracktitle": "Song",
            "artist": ["A1", None, "A2"],
            "bpm": 120,
            "genre": "x",
            "unlisted": "ignored",
        },
        bad_reads={"genre"},
    )

    snap = io.read("a.mp3")

    assert snap.tags == {"tracktitle": "Song", "artist": "A1, A2", "bpm": "120"}
    assert snap.has_artwork is False
    (msg,) = log.error.call_args.args
    assert "failed reading genre" in msg


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (_TagFile({"artwork": b"jpeg"}), True),
        (_TagFile({"artwork": b""}), False),
        (_TagFile({"artwork": b"jpeg"}, bad_reads={"artwork"}), False),
    ],
    ids=["present", "empty", "unreadable"],
)
def test_music_tag_io_read_reports_artwork(tag_io, file, expected):
    _, io, files, _ = tag_io
    files["a.mp3"] = file

    assert io.read("a.mp3").has_artwork is expected


def test_music_tag_io_write_maps_aliases_and_skips_unset_values(tag_io):
    _, io, files, _ = tag_io
    f = files["a.mp3"] = _TagFile()

    io.write(
        "a.mp3",
        {
            "tracktitle": "Song",
            "albumartist": "AA",
            "date": "2021",
            "track_number": 3,
            "discnumber": 1,
            "genre": None,
            "unknown": "ignored",
        },
    )

    assert dict(f) == {
        "tracktitle": "Song",
        "albumartist": "AA",
        "year": "2021",
        "tracknumber": "3",
        "discnumber": "1",
    }
    assert f.saves == 1


def test_music_tag_io_write_logs_a_rejected_field_and_still_saves(tag_io):
    _, io, files, log = tag_io
    f = files["a.mp3"] = _TagFile(bad_writes={"bpm"})

    io.write("a.mp3", {"title": "Song", "bpm": "fast"})

    assert dict(f) == {"tracktitle": "Song"}
    assert f.saves == 1
    (msg,) = log.error.call_args.args
    assert "failed setting bpm='fast'" in msg


def test_music_tag_io_write_without_compat_leaves_id3_alone(tag_io):
    mod, io, files, _ = tag_io
    files["a.mp3"] = _TagFile()

    io.write("a.mp3", {"title": "Song", "year": "2020"})

    assert mod.ID3.instances == []


def test_virtualdj_compat_saves_id3v23_with_a_four_digit_year(tag_io):
    mod, io, files, _ = tag_io
    files["a.MP3"] = _TagFile()

    io.write("a.MP3", {"year": "2020-05-01"}, ensure_virtualdj_compat=True)

    (id3,) = mod.ID3.instances
    assert id3.path == "a.MP3"
    assert id3.frames == {"TYER": [(3, "2020")], "TDRC": [(3, "2020")]}
    assert id3.saved == ("a.MP3", 3)


@pytest.mark.parametrize("year", [None, "", "  ", "c. 1999", "99"])
def test_virtualdj_compat_writes_no_year_frames_for_an_unusable_year(tag_io, year):
    mod, io, files, _ = tag_io
    files["a.mp3"] = _TagFile()

    io.write("a.mp3", {"year": year, "title": "T"}, ensure_virtualdj_compat=True)

    (id3,) = mod.ID3.instances
    assert id3.frames == {}
    assert id3.saved == ("a.mp3", 3)


def test_virtualdj_compat_starts_a_new_tag_when_none_can_be_loaded(tag_io):
    mod, io, files, _ = tag_io
    mod.ID3.fail_on_load = True
    files["a.mp3"] = _TagFile()

    io.write("a.mp3", {"year": "2019"}, ensure_virtualdj_compat=True)

    (id3,) = mod.ID3.instances
    assert id3.path is None
    assert id3.saved == ("a.mp3", 3)


def test_virtualdj_compat_is_skipped_for_non_mp3_files(tag_io):
    mod, io, files, _ = tag_io
    files["a.flac"] = _TagFile()

    io.write("a.flac", {"year": "2019"}, ensure_virtualdj_compat=True)

    assert mod.ID3.instances == []
    assert files["a.flac"].saves == 1


def test_virtualdj_compat_failure_does_not_fail_the_write(tag_io):
    mod, io, files, _ = tag_io
    mod.ID3.fail_on_save = True
    files["a.mp3"] = _TagFile()

    io.write("a.mp3", {"year": "2019"}, ensure_virtualdj_compat=True)

    assert files["a.mp3"]["year"] == "2019"
    assert files["a.mp3"].saves == 1


def test_dump_tags_lists_curated_fields_then_extras_sorted(tag_io):
    mod, io, files, _ = tag_io
    files["a.mp3"] = _TagFile(
        {
            "tracktitle": "Song",
            "artist": ["A1", None, "A2"],
            "comment": None,
            "zeta": "z",
            "alpha": ["x", "y"],
            "empty": None,
            "artwork": b"jpeg",
        }
    )

    out = io.dump_tags("a.mp3")

    assert list(out)[: len(mod.TAG_FIELDS)] == mod.TAG_FIELDS
    assert out["tracktitle"] == "Song"
    assert out["artist"] == "A1, A2"
    assert out["comment"] == ""
    assert out["album"] == ""  # missing curated field
    assert list(out)[len(mod.TAG_FIELDS) :] == ["alpha", "empty", "zeta"]
    assert out["alpha"] == "x, y"
    assert out["empty"] == ""
    assert "artwork" not in out


def test_dump_tags_skips_unreadable_extras_and_survives_iteration_failure(tag_io):
    mod, io, files, _ = tag_io
    files["a.mp3"] = _TagFile({"extra": "x", "bad": "y"}, bad_reads={"bad"})
    files["b.mp3"] = _TagFile({"extra": "x"}, bad_iter=True)

    a = io.dump_tags("a.mp3")
    b = io.dump_tags("b.mp3")

    assert a["extra"] == "x"
    assert "bad" not in a
    assert list(b) == mod.TAG_FIELDS


def test_dump_tags_returns_empty_when_the_file_cannot_be_loaded(tag_io):
    _, io, files, log = tag_io
    files["/music/broken.mp3"] = RuntimeError("not an audio file")

    assert io.dump_tags("/music/broken.mp3") == {}
    (msg,) = log.error.call_args.args
    assert "broken.mp3" in msg and "not an audio file" in msg
