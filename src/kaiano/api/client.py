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
      KAIANO_API_CLERK_TOKEN — optional Clerk JWT; when set (or passed via
                               ``clerk_token``), requests use
                               ``Authorization: Bearer <token>`` instead of
                               ``X-Owner-Id``.
      KAIANO_API_OWNER_ID — owner ID passed as X-Owner-Id header when no Clerk
                            token is configured; falls back to OWNER_ID if not set

    Auth: Prefer Clerk JWT when ``KAIANO_API_CLERK_TOKEN`` is set (or when
    ``clerk_token`` is passed). Otherwise use ``X-Owner-Id`` for local dev and
    backward compatibility with internal service-to-service calls. Do not expose
    this client to untrusted callers.
    """

    def __init__(
        self,
        base_url: str | None = None,
        owner_id: str | None = None,
        clerk_token: str | None = None,
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
        resolved_token = (
            clerk_token
            if clerk_token is not None
            else os.environ.get("KAIANO_API_CLERK_TOKEN")
        )
        self.clerk_token = resolved_token if resolved_token else None
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_env(cls) -> KaianoApiClient:
        return cls()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.clerk_token:
            headers["Authorization"] = f"Bearer {self.clerk_token}"
        else:
            headers["X-Owner-Id"] = self.owner_id
        return headers

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
