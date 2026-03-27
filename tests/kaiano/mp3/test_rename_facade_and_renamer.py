import os


def test_safe_filename_component_normalizes_unicode_and_strips_chars():
    from kaiano.mp3.rename.io.rename_fs import safe_filename_component, safe_str

    assert safe_str(None) == ""
    assert safe_str("None") == ""
    assert safe_filename_component("Beyoncé Knowles") == "beyonceknowles"
    assert safe_filename_component(" A/B ") == "a_b"
    assert safe_filename_component("!!!") == ""


def test_rename_facade_build_filename_is_side_effect_free(tmp_path):
    from kaiano.mp3.rename.io.rename_fs import RenameFacade

    f = tmp_path / "My Song.mp3"
    f.write_text("x")

    facade = RenameFacade()

    # New API
    name = facade.build_filename(str(f), title="My Song", artist="The Artist")
    assert name.endswith(".mp3")

    # Legacy alias returns the same string
    name2 = facade.rename(str(f), title="My Song", artist="The Artist")
    assert name2 == name

    # No filesystem side effects
    assert os.path.exists(str(f))


def test_mp3_renamer_rename_returns_filename_and_has_no_filesystem_side_effects(
    tmp_path,
):
    from kaiano.mp3.rename.renamer import Mp3Renamer

    f = tmp_path / "orig.mp3"
    f.write_text("x")

    r = Mp3Renamer()
    name = r.rename(str(f), metadata={"title": "T", "artist": "A"})

    # Renamer returns a destination *filename* only (no rename on disk).
    assert name.endswith(".mp3")
    assert "T" in name or "t" in name
    assert "A" in name or "a" in name

    # Original file remains unchanged.
    assert os.path.exists(str(f))


def test_mp3_renamer_sanitize_string_collapses_whitespace_and_strips_special_chars():
    from kaiano.mp3.rename.renamer import Mp3Renamer

    s = Mp3Renamer.sanitize_string

    assert s(None) == ""
    assert s("") == ""
    assert s("   ") == ""
    assert s("Alice   Leader") == "Alice_Leader"
    assert s("\tAlice\nLeader\r") == "Alice_Leader"
    assert s(" A/B ") == "AB"  # slash removed
    assert s("Beyoncé Knowles") == "Beyonc_Knowles"  # non-ascii stripped
    assert s("!!!") == ""
    assert s("__a__") == "a"  # strip leading/trailing underscores


def test_mp3_renamer_build_routine_filename_uses_strict_sanitization():
    from kaiano.mp3.rename.renamer import Mp3Renamer

    out = Mp3Renamer.build_routine_filename(
        leader="Alice   Leader",
        follower="Bob/Follower",
        division="Novice",
        routine="My Routine!!!",
        descriptor="  Finals  ",
        season_year="2026",
    )

    assert out.startswith("Alice_Leader_BobFollower_Novice_")
    assert out.endswith("2026_My_Routine_Finals")


def test_mp3_renamer_build_routine_filename_omits_empty_optional_tail_parts():
    from kaiano.mp3.rename.renamer import Mp3Renamer

    out = Mp3Renamer.build_routine_filename(
        leader="Alice",
        follower="Bob",
        division="Novice",
        routine="",  # omitted
        descriptor=None,  # omitted
        season_year="2026",
    )

    assert out == "Alice_Bob_Novice_2026"
