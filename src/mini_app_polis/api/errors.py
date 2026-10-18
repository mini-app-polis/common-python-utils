from __future__ import annotations

import html
import json
import re

#: Status codes a proxy in front of the API answers with while nothing
#: behind it is serving: a deploy swap, a crash-restart, an edge incident.
_UNAVAILABLE_STATUSES = frozenset({502, 503, 504})


class KaianoApiError(Exception):
    """Represent an HTTP failure returned by a Kaiano API endpoint."""

    def __init__(self, status_code: int, message: str, path: str):
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"KaianoApiError {status_code} on {path}: {message}")


class ApiUnavailable(KaianoApiError):
    """The API could not be reached; the request never got an answer from it.

    Raised when every connection attempt failed, when a proxy answers 502,
    503 or 504, and when Railway's edge answers for a domain with no running
    deployment behind it. That last one is a 404 — "Application not found" —
    and indistinguishable by status alone from a route the API does not have,
    which is a bug and should stay loud. The body tells them apart: the API's
    own errors are an ``{"error": {...}}`` envelope, the edge's is not.

    A subclass, so every ``except KaianoApiError`` in the fleet still catches
    it. Catch this one first where "try again later" is the right answer.
    """


def is_edge_not_found(status_code: int, body: str) -> bool:
    """Whether a response is Railway's edge saying no deployment is serving."""
    if status_code != 404:
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return (
        isinstance(parsed, dict)
        and "error" not in parsed
        and parsed.get("message") == "Application not found"
    )


#: The most of a body that is not the API's JSON an error message keeps.
_MAX_BODY_CHARS = 300

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
#: Cloudflare's error pages name the request: "Ray ID: <strong>a4c9…</strong>".
_RAY_ID = re.compile(r"Ray ID:\s*(?:<[^>]+>\s*)*([0-9a-f]{12,})", re.IGNORECASE)


def summarize_body(body: str) -> str:
    """A response body short enough for an exception, a log line or a report.

    The API's own errors are a short JSON envelope and are kept whole. Anything
    else comes from in front of the API — Cloudflare's or Railway's error page
    — and its HTML used to be quoted in full, a page of markup in a Discord run
    report. An HTML page is reduced to its title and, when Cloudflare served it,
    its Ray ID (what Cloudflare support asks for); other text is cut short.
    """
    text = body.strip()
    if not text:
        return "(empty body)"
    if text[0] in "{[":
        return text
    if "<html" in text[:500].lower() or text[:15].lower().startswith("<!doctype"):
        found = _TITLE.search(text)
        title = " ".join(html.unescape(found.group(1)).split()) if found else ""
        ray = _RAY_ID.search(text)
        parts = [f"HTML error page: {title}" if title else "HTML error page"]
        if ray:
            parts.append(f"Cloudflare ray {ray.group(1)}")
        return ", ".join(parts)
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS].rstrip()}… ({len(text)} chars)"
    return text


def error_for(status_code: int, body: str, path: str) -> KaianoApiError:
    """The exception for a non-2xx response: unavailable, or the API's refusal.

    The message is :func:`summarize_body` of the body, never a whole HTML page.
    """
    message = summarize_body(body)
    if status_code in _UNAVAILABLE_STATUSES or is_edge_not_found(status_code, body):
        return ApiUnavailable(status_code=status_code, message=message, path=path)
    return KaianoApiError(status_code=status_code, message=message, path=path)
