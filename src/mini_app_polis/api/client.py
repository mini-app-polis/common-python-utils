"""HTTP client for Kaiano internal APIs.

**Auth:** Sends ``Authorization: Bearer <key>`` using this caller's own named
API key. No token exchange, no issuer on the request path — the key is the
credential and the receiving service matches it against configuration.

**Identity:** a cog presents its own named API key as a bearer credential.
The key identifies the machine — the receiving service matches it against the
keys it holds in configuration — so nothing is asserted by the caller, no
token is minted, and no identity provider sits on the request path.

The key proves *who* the caller is and never what it may do. Permissions are
decided by the receiving service from its own declaration, and nothing sent
here can widen them.

The shared Clerk machine secret this replaced is gone. Every cog holding one
key indistinguishable from every other cog's was the reason the API could tell
that *a* cog called it and never which one; keeping it as a fallback would
have kept that ambiguity available.

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
"""

from __future__ import annotations

import logging as _logging
import os
from typing import Any

import httpx

from .errors import KaianoApiError

_log = _logging.getLogger(__name__)


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


class KaianoApiClient:
    """
    HTTP client for calling Kaiano's internal FastAPI services.

    Reads configuration from environment variables:
      KAIANO_API_BASE_URL             — base URL of the target service
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        api_key: str | None = None,
        machine_name: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("KAIANO_API_BASE_URL", "")).rstrip(
            "/"
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
        """Auth headers for API requests.

        The key is used directly — no token exchange, so no network call
        before the call you wanted to make, and nothing to cache or refresh.
        """
        if not self.api_key:
            raise KaianoApiError(
                status_code=0,
                message=(
                    "No API key: set this machine's key (see machine_key_env_var) "
                    "or KAIANO_API_KEY"
                ),
                path="",
            )
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
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
                    # `params` is passed only when there is something to
                    # pass. httpx *replaces* a URL's query string whenever
                    # the kwarg is supplied at all, so the previous
                    # unconditional `params=query` — where query was `{}`
                    # for every caller that had no params — silently
                    # discarded any query string written into `path`.
                    #
                    # A caller asking for `/v1/evaluations?repo=x&limit=1`
                    # got `/v1/evaluations`: no filter, default limit, the
                    # newest row across every repo. It returned 200 with
                    # plausible data, so nothing looked wrong anywhere.
                    if query:
                        response = client.get(
                            url, params=query, headers=self._headers()
                        )
                    else:
                        response = client.get(url, headers=self._headers())

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
