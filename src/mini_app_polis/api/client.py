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
        # CURRENT: X-Owner-Id header for internal processor-to-API calls.
        # No real security — intended for trusted internal use only.
        #
        # FUTURE: Replace with Clerk M2M token when real user auth is needed:
        #   1. Create a JWT Template in the Clerk dashboard
        #   2. Use Clerk Backend SDK to issue short-lived tokens
        #   3. Cache the token until expiry (typically 1 hour)
        #   4. Send as Authorization: Bearer <token> instead of X-Owner-Id
        #
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
