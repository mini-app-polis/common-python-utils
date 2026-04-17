"""HTTP client for Kaiano internal APIs.

**Current auth (Project Keystone Phase 2):**
Sends ``Authorization: Bearer <m2m_jwt>`` when ``KAIANO_API_CLERK_MACHINE_SECRET``
is set. The JWT is created via Clerk's M2M token API using the machine secret key
for the ``miniappolis-cogs`` machine, cached until 60 seconds before expiry,
and refreshed automatically.

Falls back to ``X-Owner-Id`` (legacy) when the machine secret is not configured.
This fallback is removed in Phase 3 when ``flags.keystone.legacy_auth_enabled``
is flipped to FALSE.

**Env vars:**
  KAIANO_API_BASE_URL             — base URL of the target API service
  KAIANO_API_CLERK_MACHINE_SECRET — ak_xxx secret for miniappolis-cogs machine
  KAIANO_API_OWNER_ID             — legacy fallback owner ID (X-Owner-Id era)
  OWNER_ID                        — legacy fallback owner ID (older convention)
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

from .errors import KaianoApiError

# ---------------------------------------------------------------------------
# Clerk M2M token cache (module-level, thread-safe)
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_cached_token: str | None = None
_token_expires_at: float = 0.0  # monotonic time
_REFRESH_BUFFER_SECS = 60.0  # refresh this many seconds before expiry


def _create_clerk_m2m_token(machine_secret: str) -> tuple[str, float]:
    """
    Exchange the machine secret key for a short-lived Clerk M2M JWT.

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
        raise KaianoApiError(
            status_code=resp.status_code,
            message=f"Clerk M2M token creation failed: {resp.text}",
            path="/v1/m2m_tokens",
        )

    data = resp.json()
    token: str = data["token"]

    # Parse expiry from the JWT payload (base64url middle segment)
    import base64
    import json as _json

    parts = token.split(".")
    if len(parts) == 3:
        # Add padding
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(padded))
        exp: int = payload.get("exp", 0)
        # Convert unix timestamp to monotonic equivalent
        unix_now = time.time()
        mono_now = time.monotonic()
        expires_at = mono_now + max(0.0, exp - unix_now)
    else:
        # Opaque token — Clerk returns expiry separately
        expires_in: int = data.get("expires_in", 3600)
        expires_at = time.monotonic() + expires_in

    return token, expires_at


def _get_m2m_token(machine_secret: str) -> str:
    """Return a cached M2M JWT, refreshing if within the buffer window."""
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
      KAIANO_API_CLERK_MACHINE_SECRET — Clerk M2M machine secret (preferred)
      KAIANO_API_OWNER_ID             — legacy X-Owner-Id fallback
      OWNER_ID                        — legacy X-Owner-Id fallback (older)
    """

    def __init__(
        self,
        base_url: str | None = None,
        owner_id: str | None = None,
        machine_secret: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = (base_url or os.environ.get("KAIANO_API_BASE_URL", "")).rstrip(
            "/"
        )
        self.owner_id = (
            owner_id
            or os.environ.get("KAIANO_API_OWNER_ID")
            or os.environ.get("OWNER_ID", "dev-owner")
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
        """
        Returns auth headers for API requests.

        Prefers Clerk M2M JWT (Authorization: Bearer) when machine secret is
        available. Falls back to X-Owner-Id for legacy compatibility.
        """
        if self.machine_secret:
            token = _get_m2m_token(self.machine_secret)
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            }
        # Legacy fallback — remove when flags.keystone.legacy_auth_enabled = FALSE
        return {
            "Content-Type": "application/json",
            "X-Owner-Id": self.owner_id,
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
