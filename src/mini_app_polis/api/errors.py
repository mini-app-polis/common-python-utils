from __future__ import annotations

import json

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


def error_for(status_code: int, body: str, path: str) -> KaianoApiError:
    """The exception for a non-2xx response: unavailable, or the API's refusal."""
    if status_code in _UNAVAILABLE_STATUSES or is_edge_not_found(status_code, body):
        return ApiUnavailable(status_code=status_code, message=body, path=path)
    return KaianoApiError(status_code=status_code, message=body, path=path)
