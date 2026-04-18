"""HTTP client for Kaiano internal APIs.

**Auth:** Sends ``Authorization: Bearer <token>`` using a Clerk M2M opaque
token created from the ``miniappolis-cogs`` machine secret key. The token
is cached until 60 seconds before expiry and refreshed automatically.

**Env vars:**
  KAIANO_API_BASE_URL             — base URL of the target API service
  KAIANO_API_CLERK_MACHINE_SECRET — machine secret key for miniappolis-cogs
"""

from __future__ import annotations

import logging as _logging
import os
import threading
import time
from typing import Any

import httpx

from .errors import KaianoApiError

_log = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clerk M2M token cache (module-level, thread-safe)
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_cached_token: str | None = None
_token_expires_at: float = 0.0  # monotonic time
_REFRESH_BUFFER_SECS = 60.0  # refresh this many seconds before expiry


def _create_clerk_m2m_token(machine_secret: str) -> tuple[str, float]:
    """
    Exchange the machine secret key for a Clerk M2M opaque token.

    Returns (token_string, expires_at_monotonic).
    Raises KaianoApiError on failure.
    """
    url = "https://api.clerk.com/v1/m2m_tokens"
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {machine_secret}",
                "Content-Type": "application/json",
            },
            json={},
        )

    if resp.status_code >= 400:
        _log.warning(
            "[m2m] token creation failed status=%s body=%s secret_prefix=%s",
            resp.status_code,
            resp.text[:300],
            machine_secret[:8] + "..." if machine_secret else "MISSING",
        )
        raise KaianoApiError(
            status_code=resp.status_code,
            message=f"Clerk M2M token creation failed: {resp.text}",
            path="/v1/m2m_tokens",
        )

    data = resp.json()
    token: str = data["token"]

    # Opaque token — Clerk returns expiry as expires_in seconds
    expires_in: int = data.get("expires_in", 3600)
    expires_at = time.monotonic() + expires_in

    _log.info("[m2m] token created expires_in=%s", expires_in)
    return token, expires_at


def _get_m2m_token(machine_secret: str) -> str:
    """Return a cached M2M token, refreshing if within the buffer window."""
    global _cached_token, _token_expires_at

    with _token_lock:
        now = time.monotonic()
        if _cached_token is None or now >= (_token_expires_at - _REFRESH_BUFFER_SECS):
            token, expires_at = _create_clerk_m2m_token(machine_secret)
            _cached_token = token
            _token_expires_at = expires_at
        return _cached_token


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class KaianoApiClient:
    """
    HTTP client for calling Kaiano's internal FastAPI services.

    Reads configuration from environment variables:
      KAIANO_API_BASE_URL             — base URL of the target service
      KAIANO_API_CLERK_MACHINE_SECRET — Clerk M2M machine secret
    """

    def __init__(
        self,
        base_url: str | None = None,
        machine_secret: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = (base_url or os.environ.get("KAIANO_API_BASE_URL", "")).rstrip(
            "/"
        )
        self.machine_secret = machine_secret or os.environ.get(
            "KAIANO_API_CLERK_MACHINE_SECRET"
        )
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_env(cls) -> KaianoApiClient:
        """Build a client using environment-based configuration defaults."""
        return cls()

    def _headers(self) -> dict[str, str]:
        """Returns auth headers for API requests."""
        if not self.machine_secret:
            raise KaianoApiError(
                status_code=0,
                message="KAIANO_API_CLERK_MACHINE_SECRET is not set",
                path="",
            )
        token = _get_m2m_token(self.machine_secret)
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Make a synchronous POST request to the API.

        Retries up to max_retries times on connection errors.
        Raises KaianoApiError on non-2xx responses.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload, headers=self._headers())

                if response.status_code >= 400:
                    raise KaianoApiError(
                        status_code=response.status_code,
                        message=response.text,
                        path=path,
                    )

                return response.json()

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                continue

        raise KaianoApiError(
            status_code=0,
            message=f"Connection failed after {self.max_retries} attempts: {last_exc}",
            path=path,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Make a synchronous GET request to the API.

        Retries up to max_retries times on connection errors.
        Raises KaianoApiError on non-2xx responses.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        query = params or {}

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, params=query, headers=self._headers())

                if response.status_code >= 400:
                    raise KaianoApiError(
                        status_code=response.status_code,
                        message=response.text,
                        path=path,
                    )

                return response.json()

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                continue

        raise KaianoApiError(
            status_code=0,
            message=f"Connection failed after {self.max_retries} attempts: {last_exc}",
            path=path,
        )
