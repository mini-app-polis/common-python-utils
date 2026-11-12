"""Rule profiles on real files: main writes the catalog's values, fx sends the
file's up, neither ever empties a value, and a dry run writes nothing."""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from mini_app_polis.mp3.tag import (
    FX,
    MAIN,
    PROFILES,
    AudioTagger,
    FieldChange,
    RuleProfile,
    UnsupportedReason,
    Winner,
    apply_profile,
    get_profile,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tagger(monkeypatch: pytest.MonkeyPatch) -> AudioTagger:
    """An AudioTagger on the real music_tag and mutagen.id3 (older tests in
    this folder put stand-ins for both in sys.modules)."""
    for name in ("music_tag", "mutagen.id3"):
        if not getattr(sys.modules.get(name), "__file__", None):
            monkeypatch.delitem(sys.modules, name, raising=False)
    music_tag = importlib.import_module("music_tag")
    id3 = importlib.import_module("mutagen.id3")
    io_mod = importlib.import_module("mini_app_polis.mp3.tag.io.music_tag_io")
    monkeypatch.setattr(io_mod, "music_tag", music_tag)
    for attr in ("ID3", "TDRC", "TYER", "ID3NoHeaderError"):
        monkeypatch.setattr(io_mod, attr, getattr(id3, attr))
    return AudioTagger(io=io_mod.MusicTagIO())


FILE_TAGS = {
    "title": "temple",
    "artist": "Thomas Gold",
    "year": "2019",
    "genre": "House",
    "bpm": 124,
}

CATALOG = {
    "title": "Temple (Niko The Kid Remix)",
    "artist": "Thomas Gold",
    "year": 2019,
    "genre": "WCS",
    "bpm": 124.0,
    "label": "Spinnin",
}


def _song(tagger: AudioTagger, tmp_path: Path, fixture: str = "tone.flac") -> str:
    path = tmp_path / fixture
    shutil.copyfile(FIXTURES / fixture, path)
    tagger.write(str(path), FILE_TAGS)
    return str(path)


def test_the_main_and_fx_profiles():
    assert PROFILES == {"main": MAIN, "fx": FX}
    assert MAIN.winner is Winner.CATALOG and MAIN.display == "catalog"
    assert FX.winner is Winner.FILE and FX.display == "file"
    assert get_profile("fx") is FX
    with pytest.raises(ValueError, match="known"):
        get_profile("routine")
    with pytest.raises(ValueError, match="unknown fields"):
        RuleProfile("odd", fields=("mood",), winner=Winner.FILE, display="file")


@pytest.mark.parametrize("fixture", ["tone.mp3", "tone.flac", "tone.m4a", "tone.ogg"])
def test_main_writes_the_catalogs_values(tagger, tmp_path, fixture):
    path = _song(tagger, tmp_path, fixture)

    result = apply_profile(path, MAIN, CATALOG, tagger=tagger)

    assert result.written is True
    assert result.to_catalog == ()
    assert {c.field for c in result.to_file} == {"title", "genre", "label"}
    tags = tagger.read(path).tags
    assert tags["tracktitle"] == "Temple (Niko The Kid Remix)"
    assert tags["genre"] == "WCS"
    assert tags["label"] == "Spinnin"
    assert tags["bpm"] == "124"
    # Applied again, nothing differs.
    assert apply_profile(path, MAIN, CATALOG, tagger=tagger).to_file == ()


def test_main_never_empties_a_tag(tagger, tmp_path):
    path = _song(tagger, tmp_path)

    result = apply_profile(path, MAIN, {**CATALOG, "genre": None}, tagger=tagger)

    assert "genre" not in {c.field for c in result.to_file}
    assert tagger.read(path).tags["genre"] == "House"


def test_a_dry_run_writes_nothing(tagger, tmp_path):
    path = _song(tagger, tmp_path)
    before = Path(path).read_bytes()

    result = apply_profile(path, MAIN, CATALOG, dry_run=True, tagger=tagger)

    assert result.written is False
    assert result.to_file
    assert Path(path).read_bytes() == before


def test_fx_keeps_the_file_and_sends_its_values_up(tagger, tmp_path):
    path = _song(tagger, tmp_path)
    before = Path(path).read_bytes()

    result = apply_profile(path, FX, CATALOG, tagger=tagger)

    assert result.written is False
    assert result.to_file == ()
    assert set(result.to_catalog) == {
        FieldChange("title", "temple", "Temple (Niko The Kid Remix)"),
        FieldChange("genre", "House", "WCS"),
    }
    assert Path(path).read_bytes() == before


def test_fx_never_empties_an_entrys_value(tagger, tmp_path):
    path = _song(tagger, tmp_path)

    result = apply_profile(path, FX, CATALOG, tagger=tagger)

    # The file has no label: the entry keeps its own.
    assert "label" not in {c.field for c in result.to_catalog}


def test_a_file_it_cannot_tag_is_reported_and_left_alone(tagger, tmp_path):
    path = tmp_path / "song.flac.part"
    shutil.copyfile(FIXTURES / "tone.flac", path)
    before = path.read_bytes()

    result = apply_profile(str(path), MAIN, CATALOG, tagger=tagger)

    assert result.unsupported is not None
    assert result.unsupported.reason is UnsupportedReason.PARTIAL_DOWNLOAD
    assert result.to_file == () and result.written is False
    assert path.read_bytes() == before
