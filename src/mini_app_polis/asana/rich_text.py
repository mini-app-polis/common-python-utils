"""Helpers for composing Asana rich text.

Asana's ``notes`` field is plain text; ``html_notes`` is a restricted
HTML subset that must be wrapped in a single ``<body>`` element. The
supported tags are ``<strong> <em> <u> <s> <code> <ol> <ul> <li> <a>
<blockquote> <pre>`` universally, plus ``<h1> <h2> <hr/>`` on tasks.
Anything else, or malformed markup, is a 400.

That makes escaping a correctness concern rather than a nicety: task
bodies here are assembled from transcripts and model output, where a
stray ``<`` or ``&`` is ordinary. These two functions exist so every
caller escapes the same way instead of each one remembering to.

Reference: https://developers.asana.com/docs/rich-text
"""

from __future__ import annotations

from html import escape


def escape_rich_text(value: str) -> str:
    """Escape caller-supplied text for inclusion in ``html_notes``.

    Escapes ``&``, ``<`` and ``>``. Quotes are left alone — this is
    for text nodes, not attribute values; use :func:`link` for the one
    place an attribute is needed.
    """
    return escape(value, quote=False)


def link(url: str, text: str) -> str:
    """Render an ``<a>`` element with both parts escaped.

    ``url`` is escaped with quotes on because it lands in an attribute.
    """
    return f'<a href="{escape(url, quote=True)}">{escape_rich_text(text)}</a>'


def rich_text_body(*sections: str) -> str:
    """Join pre-rendered sections with blank lines and wrap in ``<body>``.

    Sections are joined verbatim — they are expected to be already
    escaped, since a section is usually a mix of markup and text and
    only the caller knows which is which. Empty sections are dropped so
    an absent field leaves no blank gap.

    Asana honours literal newlines inside ``<body>``, so paragraphs are
    separated with ``\\n\\n`` rather than ``<p>`` tags (which the
    supported-tag list does not include).
    """
    body = "\n\n".join(section for section in sections if section)
    return f"<body>{body}</body>"
