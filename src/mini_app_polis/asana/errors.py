"""Errors raised by :mod:`mini_app_polis.asana`.

The split is by what the operator has to do about it, not by status
code. ``AsanaAuthError`` means a human must rotate a credential, so a
retry can only fail the same way; ``AsanaAPIError`` means the call
might succeed if it runs again. Callers running under Prefect map the
two accordingly: raise-and-stop versus raise-and-retry.
"""

from __future__ import annotations


class AsanaError(RuntimeError):
    """Base error for :mod:`mini_app_polis.asana`."""


class AsanaAuthError(AsanaError):
    """The access token is missing, invalid, or revoked (401/403).

    Retrying cannot help. The fix is to mint a new personal access
    token at https://app.asana.com/0/my-apps and rotate
    ``ASANA_ACCESS_TOKEN`` in Doppler.
    """


class AsanaAPIError(AsanaError):
    """A non-auth API failure — 4xx other than 401/403, 5xx, or a
    response whose shape does not match what the endpoint documents.

    Retry-eligible.
    """

    def __init__(self, status_code: int | None, message: str, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.path = path
        code = status_code if status_code is not None else "?"
        super().__init__(f"AsanaAPIError {code} on {path}: {message}")
