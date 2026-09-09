"""Tests for Asana rich-text composition."""

from __future__ import annotations

from mini_app_polis.asana import escape_rich_text, link, rich_text_body


def test_escape_rich_text_neutralizes_markup_but_leaves_quotes() -> None:
    """Transcripts contain angle brackets; Asana answers 400 on bad markup."""
    assert escape_rich_text('a < b & "c"') == 'a &lt; b &amp; "c"'


def test_link_escapes_both_href_and_text() -> None:
    assert link("https://x/?a=1&b=2", "Listen") == (
        '<a href="https://x/?a=1&amp;b=2">Listen</a>'
    )


def test_rich_text_body_wraps_and_separates_sections() -> None:
    assert rich_text_body("one", "two") == "<body>one\n\ntwo</body>"


def test_rich_text_body_drops_empty_sections() -> None:
    """An absent field should leave no blank gap in the rendered body."""
    assert rich_text_body("one", "", None or "", "two") == "<body>one\n\ntwo</body>"


def test_rich_text_body_with_nothing_is_still_valid_markup() -> None:
    assert rich_text_body() == "<body></body>"
