"""The live contract suite's guards and ledger, tested without the network.

The suite itself (contract/) only runs against a deployed API. What keeps it
off production must not wait for that to be proven, so it is tested here, in
every CI run.
"""

from __future__ import annotations

from typing import Any

import pytest
from contract.harness import (
    MACHINE_NAME,
    NOT_EXERCISED,
    ContractConfig,
    ContractConfigError,
    Ledger,
    LedgerClient,
    load_config,
    preflight,
)

from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import ENDPOINTS_BY_NAME

DEV_URL = "https://dev-api.kaianolevine.com"
DEV_KEY = "dev_abc123"


def _env(**overrides: str) -> dict[str, str]:
    return {"CONTRACT_API_URL": DEV_URL, "CONTRACT_SUITE_API_KEY": DEV_KEY, **overrides}


# ── Guard 1: development key ──────────────────────────────────────────────


def test_accepts_the_development_api_with_a_development_key() -> None:
    config = load_config(_env(CONTRACT_API_URL=f"{DEV_URL}/"))

    assert config == ContractConfig(api_url=DEV_URL, api_key=DEV_KEY)


def test_names_every_missing_setting() -> None:
    with pytest.raises(ContractConfigError) as excinfo:
        load_config({"CONTRACT_API_URL": " ", "CONTRACT_SUITE_API_KEY": ""})

    assert "CONTRACT_API_URL" in str(excinfo.value)
    assert "CONTRACT_SUITE_API_KEY" in str(excinfo.value)


@pytest.mark.parametrize("key", ["prod_abc", "abc123", "DEV_abc", " dev"])
def test_refuses_a_key_that_is_not_a_development_key(key: str) -> None:
    with pytest.raises(ContractConfigError, match="not a development key"):
        load_config(_env(CONTRACT_SUITE_API_KEY=key))


# ── Guard 2: production hosts ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://api.kaianolevine.com",
        "https://kaianolevine.com",
        "https://wcs.kaianolevine.com/v1",
        "https://API.KAIANOLEVINE.COM",
        "https://anything-new.kaianolevine.com",
    ],
)
def test_refuses_a_production_host(url: str) -> None:
    with pytest.raises(ContractConfigError, match="writes data"):
        load_config(_env(CONTRACT_API_URL=url))


@pytest.mark.parametrize("url", ["dev-api.kaianolevine.com", "ftp://x", "https://"])
def test_refuses_something_that_is_not_a_url(url: str) -> None:
    with pytest.raises(ContractConfigError, match="not a URL"):
        load_config(_env(CONTRACT_API_URL=url))


def test_allows_a_host_outside_the_production_domain() -> None:
    """A local API, say. Guard 3 is what stops a production host elsewhere."""
    assert load_config(_env(CONTRACT_API_URL="http://localhost:8000")).host == (
        "localhost"
    )


# ── Guard 3: the API must know the key as this machine ────────────────────


class _Whoami:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ARG002
        assert path == "/v1/identity/whoami"
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_preflight_refuses_when_the_api_rejects_the_key() -> None:
    client = _Whoami(KaianoApiError(status_code=401, message="no", path="/"))

    with pytest.raises(ContractConfigError, match="development deployment"):
        preflight(client)  # type: ignore[arg-type]


def test_preflight_refuses_a_key_that_is_another_machine() -> None:
    client = _Whoami({"principal": {"display_name": "deejay-cog"}})

    with pytest.raises(ContractConfigError, match="another caller"):
        preflight(client)  # type: ignore[arg-type]


def test_preflight_refuses_a_key_with_no_principal() -> None:
    with pytest.raises(ContractConfigError):
        preflight(_Whoami({"principal": None}))  # type: ignore[arg-type]


def test_preflight_passes_other_errors_through() -> None:
    client = _Whoami(KaianoApiError(status_code=503, message="down", path="/"))

    with pytest.raises(KaianoApiError):
        preflight(client)  # type: ignore[arg-type]


def test_preflight_accepts_the_contract_suite_machine() -> None:
    body = {"principal": {"display_name": MACHINE_NAME}}

    assert preflight(_Whoami(body)) == body  # type: ignore[arg-type]


# ── The ledger ────────────────────────────────────────────────────────────


def test_not_exercised_names_only_catalog_endpoints() -> None:
    assert set(NOT_EXERCISED) <= set(ENDPOINTS_BY_NAME)


def test_ledger_refuses_a_name_not_in_the_catalog() -> None:
    with pytest.raises(KeyError):
        Ledger().skip("request_deejay_runs", "typo")


def test_ledger_reports_what_is_unaccounted_for() -> None:
    ledger = Ledger()
    ledger.hit("ingest")
    ledger.skip("export_wcs_wiki", "reason")
    ledger.skip("ingest", "a hit outranks a skip")

    report = ledger.report()

    assert report.exercised == ["ingest"]
    assert report.skipped == [("export_wcs_wiki", "reason")]
    assert set(report.unaccounted) == (
        set(ENDPOINTS_BY_NAME) - {"ingest", "export_wcs_wiki"}
    )


def test_ledger_client_records_only_calls_that_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[Any] = ["ok", ValueError("response outside the contract")]

    def fake_call(self, endpoint_name, body=None, params=None):  # noqa: ARG001
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(KaianoApiClient, "_call", fake_call)
    ledger = Ledger()
    client = LedgerClient(ContractConfig(api_url=DEV_URL, api_key=DEV_KEY), ledger)

    client.export_wcs_wiki()
    with pytest.raises(ValueError):
        client.list_evaluations()

    assert ledger.hits == {"export_wcs_wiki"}
    assert client.machine_name == MACHINE_NAME
