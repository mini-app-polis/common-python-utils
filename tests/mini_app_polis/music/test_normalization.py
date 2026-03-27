from __future__ import annotations

from mini_app_polis.music import normalize_for_matching


def test_basic_lowercase_and_whitespace() -> None:
    title, artist = normalize_for_matching("  Hello World  ", "  SOME ARTIST  ")
    assert title == "hello world"
    assert artist == "some artist"
    assert isinstance(title, str)
    assert isinstance(artist, str)


def test_returns_tuple_of_two_strings() -> None:
    result = normalize_for_matching("a", "b")
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(x, str) for x in result)


def test_strips_feat_variants_and_trailing_credit() -> None:
    assert normalize_for_matching("Song feat. Guest", "Main") == ("song", "main")
    assert normalize_for_matching("Song FT. Guest Star", "Main") == ("song", "main")
    assert normalize_for_matching("Song featuring Someone", "Main") == ("song", "main")


def test_strips_parenthetical_suffixes() -> None:
    assert normalize_for_matching("Track (Radio Edit)", "Art") == ("track", "art")
    assert normalize_for_matching("Track (clean)", "Art") == ("track", "art")
    assert normalize_for_matching("Track (Acoustic)", "Art") == ("track", "art")
    assert normalize_for_matching("Track (Remix)", "Art") == ("track", "art")
    assert normalize_for_matching("Track (Original Mix)", "Art") == ("track", "art")
    assert normalize_for_matching("Track (Clean Version)", "Art") == ("track", "art")


def test_preserves_interior_punctuation_like_apostrophes() -> None:
    t, a = normalize_for_matching("Don't Stop", "Flo Rida")
    assert t == "don't stop"
    assert a == "flo rida"
    t2, a2 = normalize_for_matching("Song", "O'Connor")
    assert t2 == "song"
    assert a2 == "o'connor"


def test_already_clean_input_unmangled() -> None:
    t, a = normalize_for_matching("midnight city", "m83")
    assert t == "midnight city"
    assert a == "m83"
