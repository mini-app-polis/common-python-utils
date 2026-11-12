"""AudioTagger on real files: a tag round trip in every supported format.

The compressed fixtures in ``fixtures/`` are 0.05 s of a 440 Hz tone at
8 kHz mono, made with ffmpeg and written with no tags; WAV and AIFF are
built here. Each round trip also checks the audio is unchanged: the bytes of
the file outside its tag block, taken apart per format (see
``_audio_payload``), and mutagen's stream info.
"""

from __future__ import annotations

import importlib
import math
import shutil
import struct
import sys
import wave
import zipfile
from pathlib import Path

import pytest

from mini_app_polis.mp3.tag import (
    SUPPORTED_FORMATS,
    AudioFormat,
    AudioTagger,
    Mp3Tagger,
    TagSnapshot,
    UnsupportedFile,
    UnsupportedReason,
)

FIXTURES = Path(__file__).parent / "fixtures"

_RATE = 8000
_FRAMES = 400


def _samples() -> list[int]:
    return [int(8000 * math.sin(2 * math.pi * 440 * i / _RATE)) for i in range(_FRAMES)]


def _make_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(struct.pack(f"<{_FRAMES}h", *_samples()))


def _extended(value: int) -> bytes:
    """``value`` as the 80-bit IEEE extended float AIFF stores its rate in."""
    exponent = value.bit_length() - 1
    mantissa = value << (63 - exponent)
    return struct.pack(">HQ", 16383 + exponent, mantissa)


def _make_aiff(path: Path) -> None:
    comm = struct.pack(">hIh", 1, _FRAMES, 16) + _extended(_RATE)
    ssnd = struct.pack(">II", 0, 0) + struct.pack(f">{_FRAMES}h", *_samples())
    body = b"AIFF"
    for cid, data in ((b"COMM", comm), (b"SSND", ssnd)):
        body += cid + struct.pack(">I", len(data)) + data
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)


_SOURCES = {
    AudioFormat.MP3: "tone.mp3",
    AudioFormat.FLAC: "tone.flac",
    AudioFormat.OGG_VORBIS: "tone.ogg",
    AudioFormat.OGG_OPUS: "tone.opus",
    AudioFormat.M4A: "tone.m4a",
    AudioFormat.WAV: _make_wav,
    AudioFormat.AIFF: _make_aiff,
}
_SUFFIX = {
    AudioFormat.MP3: ".mp3",
    AudioFormat.FLAC: ".flac",
    AudioFormat.OGG_VORBIS: ".ogg",
    AudioFormat.OGG_OPUS: ".opus",
    AudioFormat.M4A: ".m4a",
    AudioFormat.WAV: ".wav",
    AudioFormat.AIFF: ".aiff",
}


def _sample(fmt: AudioFormat, directory: Path, name: str = "song") -> Path:
    path = directory / f"{name}{_SUFFIX[fmt]}"
    source = _SOURCES[fmt]
    if isinstance(source, str):
        shutil.copyfile(FIXTURES / source, path)
    else:
        source(path)
    return path


# --- the audio outside the tag block, per format ---------------------------


def _mp3_payload(data: bytes) -> bytes:
    """Everything between a leading ID3v2 tag and a trailing ID3v1 tag."""
    if data[:3] == b"ID3":
        size = 0
        for b in data[6:10]:
            size = (size << 7) | b
        footer = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer :]
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    return data


def _flac_payload(data: bytes) -> bytes:
    """The STREAMINFO block (with the audio's MD5) and every audio frame."""
    assert data[:4] == b"fLaC"
    i, streaminfo = 4, b""
    while True:
        header = data[i]
        size = int.from_bytes(data[i + 1 : i + 4], "big")
        if header & 0x7F == 0:
            streaminfo = data[i + 4 : i + 4 + size]
        i += 4 + size
        if header & 0x80:
            break
    return streaminfo + data[i:]


def _ogg_payload(path: Path) -> bytes:
    """Every packet but the comment header, and the last granule position."""
    from mutagen.ogg import OggPage

    pages = []
    with path.open("rb") as f:
        while True:
            try:
                pages.append(OggPage(f))
            except EOFError:
                break
    packets = OggPage.to_packets(pages, strict=False)
    del packets[1]
    return b"".join(packets) + struct.pack(">q", pages[-1].position)


def _iff_chunks(data: bytes, endian: str, keep: set[bytes]) -> bytes:
    """The named chunks of a RIFF (WAV) or FORM (AIFF) file, in order."""
    out, i = b"", 12
    while i + 8 <= len(data):
        cid = data[i : i + 4]
        (size,) = struct.unpack(f"{endian}I", data[i + 4 : i + 8])
        if cid in keep:
            out += cid + data[i + 8 : i + 8 + size]
        i += 8 + size + (size & 1)
    return out


def _m4a_payload(data: bytes) -> bytes:
    """The contents of every top-level ``mdat`` atom (the encoded audio)."""
    out, i = b"", 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        header = 8
        if size == 1:
            (size,) = struct.unpack(">Q", data[i + 8 : i + 16])
            header = 16
        elif size == 0:
            size = len(data) - i
        if kind == b"mdat":
            out += data[i + header : i + size]
        i += size
    return out


def _audio_payload(fmt: AudioFormat, path: Path) -> bytes:
    data = path.read_bytes()
    if fmt is AudioFormat.MP3:
        return _mp3_payload(data)
    if fmt is AudioFormat.FLAC:
        return _flac_payload(data)
    if fmt in (AudioFormat.OGG_VORBIS, AudioFormat.OGG_OPUS):
        return _ogg_payload(path)
    if fmt is AudioFormat.M4A:
        return _m4a_payload(data)
    if fmt is AudioFormat.WAV:
        with wave.open(str(path), "rb") as w:
            frames = w.readframes(w.getnframes())
            params = repr(tuple(w.getparams())).encode()
        return params + _iff_chunks(data, "<", {b"fmt ", b"data"}) + frames
    return _iff_chunks(data, ">", {b"COMM", b"SSND"})


def _stream_info(path: Path) -> tuple[object, ...]:
    import mutagen

    info = mutagen.File(str(path)).info
    return (
        type(info).__name__,
        info.length,
        getattr(info, "sample_rate", None),
        getattr(info, "channels", None),
        getattr(info, "bitrate", None),
    )


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def tagger(monkeypatch: pytest.MonkeyPatch) -> AudioTagger:
    """An AudioTagger on the real music_tag and mutagen.id3.

    Older tests in this folder put stand-ins for both in ``sys.modules`` and
    reload the tag IO module; this sets the real ones back for one test.
    """
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


METADATA = {
    "title": "Tone",
    "artist": "Test Artist",
    "album": "Test Album",
    "album_artist": "Test Album Artist",
    "year": "2021",
    "genre": "West Coast Swing",
    "comment": "a comment",
    "isrc": "USAAA2100001",
    "track_number": 3,
    "disc_number": 1,
    "bpm": 124.0,
    "label": "Test Label",
}

EXPECTED = {
    "tracktitle": "Tone",
    "artist": "Test Artist",
    "album": "Test Album",
    "albumartist": "Test Album Artist",
    "year": "2021",
    "genre": "West Coast Swing",
    "comment": "a comment",
    "isrc": "USAAA2100001",
    "tracknumber": "3",
    "discnumber": "1",
    "bpm": "124",
    "label": "Test Label",
}


# --- round trips ------------------------------------------------------------


def test_every_audio_format_has_a_sample():
    assert set(_SOURCES) == set(AudioFormat) == SUPPORTED_FORMATS


@pytest.mark.parametrize("fmt", sorted(AudioFormat))
def test_round_trip_writes_and_reads_every_field_with_the_audio_unchanged(
    tagger, tmp_path, fmt
):
    path = _sample(fmt, tmp_path)
    payload, info = _audio_payload(fmt, path), _stream_info(path)
    assert payload

    assert tagger.detect(str(path)) is fmt
    assert tagger.write(str(path), METADATA) is fmt

    snap = tagger.read(str(path))
    assert isinstance(snap, TagSnapshot)
    assert {k: snap.tags.get(k) for k in EXPECTED} == EXPECTED
    assert snap.has_artwork is False
    assert _audio_payload(fmt, path) == payload
    assert _stream_info(path) == info

    # A second write replaces the first, and still leaves the audio alone.
    assert tagger.write(str(path), {"title": "Tone 2", "genre": "Blues"}) is fmt
    snap = tagger.read(str(path))
    assert isinstance(snap, TagSnapshot)
    assert snap.tags["tracktitle"] == "Tone 2"
    assert snap.tags["genre"] == "Blues"
    assert snap.tags["artist"] == "Test Artist"
    assert _audio_payload(fmt, path) == payload
    assert _stream_info(path) == info


@pytest.mark.parametrize("fmt", sorted(AudioFormat))
def test_the_format_comes_from_the_contents_not_the_name(tagger, tmp_path, fmt):
    path = _sample(fmt, tmp_path)
    renamed = path.rename(tmp_path / "song.bin")
    assert tagger.detect(str(renamed)) is fmt


def test_an_mp3_with_no_tag_block_at_all_gets_one(tagger, tmp_path):
    path = tmp_path / "bare.mp3"
    path.write_bytes(_mp3_payload((FIXTURES / "tone.mp3").read_bytes()))
    payload = path.read_bytes()
    assert payload[:3] != b"ID3"

    assert tagger.write(str(path), METADATA) is AudioFormat.MP3
    snap = tagger.read(str(path))
    assert isinstance(snap, TagSnapshot)
    assert snap.tags["tracktitle"] == "Tone"
    assert _mp3_payload(path.read_bytes()) == payload


@pytest.mark.parametrize("compat", [False, True])
def test_an_mp3_is_written_exactly_as_mp3_tagger_writes_it(tagger, tmp_path, compat):
    by_audio = _sample(AudioFormat.MP3, tmp_path, "audio")
    by_mp3 = _sample(AudioFormat.MP3, tmp_path, "mp3")

    tagger.write(str(by_audio), METADATA, ensure_virtualdj_compat=compat)
    Mp3Tagger(io=tagger._io).write(
        str(by_mp3), METADATA, ensure_virtualdj_compat=compat
    )

    assert by_audio.read_bytes() == by_mp3.read_bytes()
    assert tagger.read(str(by_audio)) == Mp3Tagger(io=tagger._io).read(str(by_mp3))
    if compat:
        from mutagen.id3 import ID3

        assert ID3(str(by_audio)).version[:2] == (2, 3)


# --- files it leaves alone --------------------------------------------------


def _midi(path: Path) -> None:
    track = b"\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x2f\x00"
    path.write_bytes(
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, 96)
        + b"MTrk"
        + struct.pack(">I", len(track))
        + track
    )


def _xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")


@pytest.mark.parametrize(
    ("name", "make", "reason", "detail"),
    [
        (
            "song.flac.part",
            lambda p: shutil.copyfile(FIXTURES / "tone.flac", p),
            UnsupportedReason.PARTIAL_DOWNLOAD,
            "",
        ),
        (
            "SONG.MP3.PART",
            lambda p: shutil.copyfile(FIXTURES / "tone.mp3", p),
            UnsupportedReason.PARTIAL_DOWNLOAD,
            "",
        ),
        (
            "song.mp3.crdownload",
            lambda p: shutil.copyfile(FIXTURES / "tone.mp3", p),
            UnsupportedReason.PARTIAL_DOWNLOAD,
            "",
        ),
        (
            "song.m4a.partial",
            lambda p: shutil.copyfile(FIXTURES / "tone.m4a", p),
            UnsupportedReason.PARTIAL_DOWNLOAD,
            "",
        ),
        (
            "notes.txt",
            lambda p: p.write_text("set list\n"),
            UnsupportedReason.NOT_AUDIO,
            "",
        ),
        (
            "flyer.pdf",
            lambda p: p.write_bytes(b"%PDF-1.4\n%\xe2\xe3\n"),
            UnsupportedReason.NOT_AUDIO,
            "",
        ),
        ("schedule.xlsx", _xlsx, UnsupportedReason.NOT_AUDIO, ""),
        (
            "fake.mp3",
            lambda p: p.write_text("not really an mp3\n"),
            UnsupportedReason.UNREADABLE,
            None,
        ),
        (
            "empty.flac",
            lambda p: p.write_bytes(b""),
            UnsupportedReason.UNREADABLE,
            None,
        ),
        ("cue.mid", _midi, UnsupportedReason.UNSUPPORTED_FORMAT, "SMF"),
    ],
)
def test_a_file_it_cannot_tag_gets_a_typed_answer_and_is_not_touched(
    tagger, tmp_path, name, make, reason, detail
):
    path = tmp_path / name
    make(path)
    before = path.read_bytes()

    for answer in (
        tagger.detect(str(path)),
        tagger.read(str(path)),
        tagger.write(str(path), METADATA, ensure_virtualdj_compat=True),
    ):
        assert isinstance(answer, UnsupportedFile)
        assert answer.path == str(path)
        assert answer.reason is reason
        if detail is None:
            assert answer.detail
        else:
            assert answer.detail == detail

    assert path.read_bytes() == before


def test_a_missing_file_raises(tagger, tmp_path):
    with pytest.raises(FileNotFoundError):
        tagger.read(str(tmp_path / "gone.flac"))


def test_m4a_iso_timestamp_year_reads_as_its_year(tmp_path):
    """An M4A ©day holding an ISO timestamp, as some stores write it."""
    from mutagen.mp4 import MP4

    from mini_app_polis.mp3.tag.io.music_tag_io import MusicTagIO

    path = tmp_path / "tone.m4a"
    shutil.copyfile(FIXTURES / "tone.m4a", path)
    mp4 = MP4(path)
    mp4["\xa9day"] = ["2000-11-30T08:00:00Z"]
    mp4["\xa9nam"] = ["Song"]
    mp4.save()

    tags = MusicTagIO().read(str(path)).tags

    assert tags["year"] == "2000"
    assert tags["tracktitle"] == "Song"
