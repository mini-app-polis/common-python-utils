"""The FX role comment rule: <FX><Role> at the start, hand-added parts kept."""

from __future__ import annotations

import pytest

from mini_app_polis.mp3.tag import PROFILE_FIELDS, role_comment, role_conflict


@pytest.mark.parametrize(
    "comment",
    ["<FX><Walk On>", "<FX><Walk On><Competitor Routine>", "  <FX><Walk On> x"],
)
def test_already_prefixed_is_unchanged(comment: str) -> None:
    assert role_comment(comment, "Walk On") is None
    assert role_conflict(comment, "Walk On") is None


def test_other_role_is_unchanged_and_reported() -> None:
    comment = "<FX><Bow><Competitor Routine>"
    assert role_comment(comment, "Walk On") is None
    assert role_conflict(comment, "Walk On") == "Bow"


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("<FX>", "<FX><Walk On>"),
        ("<FX><Competitor Routine>", "<FX><Walk On><Competitor Routine>"),
        ("<FX> loud", "<FX><Walk On> loud"),
        ("<FX><Other", "<FX><Walk On><Other"),
    ],
)
def test_fx_without_role_gets_role_inserted(comment: str, expected: str) -> None:
    assert role_comment(comment, "Walk On") == expected
    assert role_conflict(comment, "Walk On") is None


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("loud intro", "<FX><Walk On> loud intro"),
        ("  loud  intro ", "<FX><Walk On> loud  intro "),
        ("<Competitor Routine>", "<FX><Walk On><Competitor Routine>"),
    ],
)
def test_other_text_gets_prefix(comment: str, expected: str) -> None:
    assert role_comment(comment, "Walk On") == expected
    assert role_conflict(comment, "Walk On") is None


@pytest.mark.parametrize("comment", [None, "", "   "])
def test_empty_comment(comment: str | None) -> None:
    assert role_comment(comment, "Walk On") == "<FX><Walk On>"
    assert role_conflict(comment, "Walk On") is None


def test_comment_is_not_a_profile_field() -> None:
    assert "comment" not in PROFILE_FIELDS
