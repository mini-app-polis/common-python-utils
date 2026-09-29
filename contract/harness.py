"""Plumbing for the live contract suite: configuration and its guards, the
pre-flight identity check, and the coverage ledger.

The suite writes to the API it points at, so it runs only against the
development API, only with a development key, and one run at a time (CD-033).
Three independent guards, any one of which is enough to stop it:

  1. The key must be a development key. API keys carry no environment of
     their own, so the development contract-suite key is issued with the
     ``dev_`` prefix and anything else is refused, the way deejaytools
     refuses a Clerk key that is not ``sk_test_``.
  2. The host must not be a production host. Under kaianolevine.com only the
     hosts in ``ALLOWED_DEV_HOSTS`` are allowed, so a production host added
     later is refused without a change here.
  3. Production rejects the key before the first write. The contract-suite
     machine is declared in every environment, but only development
     configuration holds its key, so anywhere else ``/v1/identity/whoami``
     answers 401 and the suite stops there.

What the suite writes is fixed, not per-run: every payload uses the same
identifiers each time, and every endpoint it writes to upserts or
deduplicates on them. A run converges on the rows the last run left, so
there is nothing to name per run and nothing to sweep when one dies.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import ENDPOINTS, ENDPOINTS_BY_NAME

#: The machine this suite authenticates as. Declared in api-kaianolevine-com's
#: identity_registry; its key is CONTRACT_SUITE_API_KEY.
MACHINE_NAME = "contract-suite"

#: The production domain. Nothing under it is called except ALLOWED_DEV_HOSTS.
PRODUCTION_DOMAIN = "kaianolevine.com"

#: The only hosts under the production domain the suite may call.
ALLOWED_DEV_HOSTS = frozenset({"dev-api.kaianolevine.com"})

#: What a development contract-suite key starts with.
DEV_KEY_PREFIX = "dev_"

#: Catalog endpoints this suite deliberately does not call, and why. The
#: ledger accepts these as accounted for; every other endpoint must be called.
NOT_EXERCISED: dict[str, str] = {
    "request_deejay_run": (
        "development has no queues: deejay-dev-jobs is not provisioned, so "
        "the API cannot enqueue the run"
    ),
    "request_transcription_run": (
        "development has no queues: transcription-dev-jobs is not "
        "provisioned, so the API cannot enqueue the job"
    ),
    "create_wcs_source": (
        "a source cannot be deleted and every source is in the unfiltered "
        "wiki export, so a fixture would stay in dev's wiki for good"
    ),
}


class ContractConfigError(RuntimeError):
    """The suite is not configured, or a guard refused the configuration."""


@dataclass(frozen=True)
class ContractConfig:
    api_url: str
    api_key: str

    @property
    def host(self) -> str:
        return urlsplit(self.api_url).hostname or ""


def load_config(env: Mapping[str, str] | None = None) -> ContractConfig:
    """Read and vet the suite's configuration, refusing unless guards 1 and 2 hold.

    Guard 3 needs the network and runs in :func:`preflight`.
    """
    source = os.environ if env is None else env
    api_url = (source.get("CONTRACT_API_URL") or "").strip()
    api_key = (source.get("CONTRACT_SUITE_API_KEY") or "").strip()

    missing = [
        name
        for name, value in (
            ("CONTRACT_API_URL", api_url),
            ("CONTRACT_SUITE_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ContractConfigError(
            f"Contract suite not configured: set {', '.join(missing)}."
        )

    if not api_key.startswith(DEV_KEY_PREFIX):
        raise ContractConfigError(
            "Refusing to run: CONTRACT_SUITE_API_KEY is not a development key "
            f"({DEV_KEY_PREFIX}…)."
        )

    parts = urlsplit(api_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ContractConfigError(
            f"Refusing to run: CONTRACT_API_URL is not a URL ({api_url!r})."
        )
    on_production_domain = host == PRODUCTION_DOMAIN or host.endswith(
        f".{PRODUCTION_DOMAIN}"
    )
    if on_production_domain and host not in ALLOWED_DEV_HOSTS:
        raise ContractConfigError(
            f"Refusing to run against {host}: the contract suite writes data."
        )

    return ContractConfig(api_url=api_url.rstrip("/"), api_key=api_key)


def preflight(client: KaianoApiClient) -> dict[str, Any]:
    """Guard 3: the API must know this key as the contract-suite machine.

    Production holds no contract-suite key, so it answers 401 here — before
    anything is written. Returns the whoami body for the run log.
    """
    try:
        body = client.get("/v1/identity/whoami")
    except KaianoApiError as exc:
        if exc.status_code == 401:
            raise ContractConfigError(
                "The API rejected the contract-suite key at /v1/identity/whoami "
                "— is CONTRACT_API_URL a development deployment, and is "
                "CONTRACT_SUITE_API_KEY set there?"
            ) from exc
        raise

    principal = body.get("principal") or {}
    if principal.get("display_name") != MACHINE_NAME:
        raise ContractConfigError(
            f"The key authenticated, but not as {MACHINE_NAME!r} "
            f"(principal: {principal!r}). Refusing to write as another caller."
        )
    return body


@dataclass(frozen=True)
class LedgerReport:
    exercised: list[str]
    skipped: list[tuple[str, str]]
    unaccounted: list[str]


@dataclass
class Ledger:
    """Every catalog endpoint ends a run exercised or skipped with a reason.

    An endpoint added to the catalog with neither fails the suite, so the
    contract cannot quietly grow an edge nothing checks.
    """

    hits: set[str] = field(default_factory=set)
    skips: dict[str, str] = field(default_factory=dict)

    def hit(self, endpoint_name: str) -> None:
        self._known(endpoint_name)
        self.hits.add(endpoint_name)

    def skip(self, endpoint_name: str, reason: str) -> None:
        self._known(endpoint_name)
        self.skips[endpoint_name] = reason

    def report(self) -> LedgerReport:
        names = [e.name for e in ENDPOINTS]
        return LedgerReport(
            exercised=[n for n in names if n in self.hits],
            skipped=[
                (n, self.skips[n])
                for n in names
                if n not in self.hits and n in self.skips
            ],
            unaccounted=[
                n for n in names if n not in self.hits and n not in self.skips
            ],
        )

    @staticmethod
    def _known(endpoint_name: str) -> None:
        if endpoint_name not in ENDPOINTS_BY_NAME:
            raise KeyError(f"{endpoint_name!r} is not in the catalog")


class LedgerClient(KaianoApiClient):
    """The shared client, recording each catalog endpoint it calls.

    Recording happens in ``_call``, the one path every typed method takes,
    and only after the response has validated against the contract. So
    "exercised" means called through the same method a cog uses, and
    answered in the shape the catalog says.
    """

    def __init__(self, config: ContractConfig, ledger: Ledger) -> None:
        super().__init__(
            base_url=config.api_url,
            api_key=config.api_key,
            machine_name=MACHINE_NAME,
        )
        self.ledger = ledger

    def _call(
        self,
        endpoint_name: str,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        out = super()._call(endpoint_name, body, params)
        self.ledger.hit(endpoint_name)
        return out
