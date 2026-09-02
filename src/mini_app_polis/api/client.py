"""HTTP client for Kaiano internal APIs.

**Auth:** Sends ``Authorization: Bearer <token>`` using a Clerk M2M opaque
token created from the ``miniappolis-cogs`` machine secret key. The token
is cached until 60 seconds before expiry and refreshed automatically.

**Identity:** a cog presents its own named API key as a bearer credential.
The key identifies the machine — the receiving service matches it against the
keys it holds in configuration — so nothing is asserted by the caller, no
token is minted, and no identity provider sits on the request path.

The key proves *who* the caller is and never what it may do. Permissions are
decided by the receiving service from its own declaration, and nothing sent
here can widen them.

Legacy: ``KAIANO_API_CLERK_MACHINE_SECRET`` mints a Clerk M2M token instead,
used by cogs that have not yet been given their own key. It authenticates as
the shared fleet machine, so calls made with it are attributable only to "a
cog". Prefer ``api_key``.

Pass ``machine_name`` and the client finds that cog's key by convention:
``transcription-cog`` -> ``TRANSCRIPTION_COG_API_KEY``. The same convention is
used by the receiving service to derive the variable it checks against, so
there is one rule rather than a mapping to keep in step on both sides.

That derivation lives here, in the shared client, rather than in each cog. A
helper copied into five repos is five things to change when the convention
does, and four of them will be missed.

**Env vars:**
  KAIANO_API_BASE_URL             — base URL of the target API service
  <MACHINE_NAME>_API_KEY          — this cog's own key (from machine_name)
  KAIANO_API_KEY                  — key for a caller that declares no name
  KAIANO_API_CLERK_MACHINE_SECRET — legacy shared machine secret
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


def machine_key_env_var(machine_name: str) -> str:
    """Environment variable holding a machine's key.

    ``deejay-cog`` -> ``DEEJAY_COG_API_KEY``. Derived from the name so the
    caller and the receiving service agree without either one carrying a
    mapping.
    """
    return f"{machine_name.upper().replace('-', '_')}_API_KEY"


def _key_for(machine_name: str | None) -> str | None:
    """This caller's key: its own variable first, then the generic one.

    A variable that exists but is blank counts as unset. It should behave like
    an absent one rather than an empty credential that fails on first use.
    """
    if machine_name:
        own = (os.environ.get(machine_key_env_var(machine_name)) or "").strip()
        if own:
            return own
    return (os.environ.get("KAIANO_API_KEY") or "").strip() or None


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
        api_key: str | None = None,
        machine_name: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("KAIANO_API_BASE_URL", "")).rstrip(
            "/"
        )
        self.machine_secret = machine_secret or os.environ.get(
            "KAIANO_API_CLERK_MACHINE_SECRET"
        )
        self.machine_name = machine_name or os.environ.get("KAIANO_API_MACHINE_NAME")
        self.api_key = api_key or _key_for(self.machine_name)
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_env(cls, machine_name: str | None = None) -> KaianoApiClient:
        """Build a client from the environment.

        Pass ``machine_name`` so this caller presents its own key rather than
        the shared fleet credential — that is what makes the receiving
        service's audit trail name which cog called.
        """
        return cls(machine_name=machine_name)

    def _headers(self) -> dict[str, str]:
        """Returns auth headers for API requests.

        A named API key is preferred and used directly — no token exchange,
        no network call before the call you wanted to make. The Clerk path
        remains for cogs still on the shared fleet secret.
        """
        if self.api_key:
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

        if not self.machine_secret:
            raise KaianoApiError(
                status_code=0,
                message=(
                    "No credential: set KAIANO_API_KEY (preferred) or "
                    "KAIANO_API_CLERK_MACHINE_SECRET"
                ),
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
