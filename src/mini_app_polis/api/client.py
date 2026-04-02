"""HTTP client for Kaiano internal APIs (X-Owner-Id era).

**Current auth:** Every request sends ``X-Owner-Id`` derived from
``KAIANO_API_OWNER_ID`` (falling back to ``OWNER_ID``). This is appropriate only
for Railway service-to-service calls on a private network.

**``KAIANO_API_CLERK_TOKEN`` is not read:** The variable appears in ``.env.example``
and the README as a forward reference, but ``KaianoApiClient._headers`` does
not use it. Setting it today has **no** effect — this is intentional
documentation debt until Clerk M2M ships, not an oversight in ``_headers``.

**Clerk M2M upgrade path:**

1. Add ``clerk-backend-api`` (or the maintained Clerk Python SDK) to package
   dependencies.
2. Issue and cache a short-lived token using a JWT Template in the Clerk
   dashboard.
3. Change ``_headers()`` to send ``Authorization: Bearer <token>`` and stop
   sending ``X-Owner-Id`` for authenticated calls.
4. Remove ``KAIANO_API_OWNER_ID`` / ``OWNER_ID`` from the client constructor
   surface; add ``CLERK_SECRET_KEY`` (or equivalent) for machine auth.

**Cross-repo coupling:** ``kaianolevine-api`` and this client must migrate
together. If the API enables ``CLERK_AUTH_ENABLED=true`` while this client still
sends only ``X-Owner-Id``, pipeline writes will return 401. Deploy updated clients
first, then flip the API flag.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .errors import KaianoApiError


class KaianoApiClient:
    """
    HTTP client for calling Kaiano's internal FastAPI services.

    Reads configuration from environment variables:
      KAIANO_API_BASE_URL — base URL of the target service
                            e.g. https://deejay-marvel-api.up.railway.app
      KAIANO_API_OWNER_ID — owner ID passed as X-Owner-Id header;
                            falls back to OWNER_ID if not set

    ``KAIANO_API_CLERK_TOKEN`` is documented for future Clerk M2M use but is
    **not** read by this class today; see the module docstring.
    """

    def __init__(
        self,
        base_url: str | None = None,
        owner_id: str | None = None,
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
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_env(cls) -> KaianoApiClient:
        return cls()

    def _headers(self) -> dict[str, str]:
        # X-Owner-Id only; KAIANO_API_CLERK_TOKEN is intentionally not read here
        # (see module docstring — forward reference until Clerk M2M lands).
        #
        # Future: Clerk Backend SDK + JWT Template → Authorization: Bearer <token>.
        # See: https://clerk.com/docs/backend-requests/making/jwt-templates
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
                    response = client.post(
                        url,
                        json=payload,
                        headers=self._headers(),
                    )

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
                    response = client.get(
                        url,
                        params=query,
                        headers=self._headers(),
                    )

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
