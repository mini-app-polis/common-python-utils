"""The live contract suite's guards and ledger, tested without the network.

The suite itself (contract/) only runs against a deployed API. What keeps it
off production must not wait for that to be proven, so it is tested here, in
every CI run.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx
import pytest
from contract.conftest import (
    BY_HAND,
    deployed_commit,
    result_message,
    result_note,
    started_by,
)
from contract.harness import (
    MACHINE_NAME,
    NOT_EXERCISED,
    ContractConfig,
    ContractConfigError,
    Ledger,
    LedgerClient,
    load_config,
    not_deployed,
    preflight,
)

from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import (
    ENDPOINTS,
    ENDPOINTS_BY_NAME,
    NotificationResult,
)

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


# ── What the development API serves ───────────────────────────────────────


def _serving(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Any], status: int = 200
) -> None:
    def get(url: str, timeout: float) -> httpx.Response:
        assert url == f"{DEV_URL}/openapi.json" and timeout > 0
        return httpx.Response(status, json={"paths": paths})

    monkeypatch.setattr(httpx, "get", get)


def _schema(*names: str) -> dict[str, Any]:
    """The schema of an API serving the named catalog endpoints, with its
    own names for path parameters."""
    paths: dict[str, Any] = {}
    for name in names:
        endpoint = ENDPOINTS_BY_NAME[name]
        path = endpoint.path.replace("{", "{api_")
        paths.setdefault(path, {})[endpoint.method.lower()] = {}
    return paths


def test_an_endpoint_the_api_serves_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serving(monkeypatch, _schema(*(e.name for e in ENDPOINTS)))

    assert not_deployed(ContractConfig(DEV_URL, DEV_KEY)) == {}


def test_an_endpoint_the_api_does_not_serve_yet_is_skipped_with_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = [e.name for e in ENDPOINTS if e.name != "report_library_file_edited"]
    _serving(monkeypatch, _schema(*served))

    skipped = not_deployed(ContractConfig(DEV_URL, DEV_KEY))

    assert list(skipped) == ["report_library_file_edited"]
    assert "dev-api.kaianolevine.com" in skipped["report_library_file_edited"]


def test_a_route_served_under_another_method_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = ENDPOINTS_BY_NAME["list_library_files"]
    _serving(monkeypatch, {endpoint.path: {"delete": {}}})

    assert "list_library_files" in not_deployed(ContractConfig(DEV_URL, DEV_KEY))


def test_routes_that_cannot_be_read_stop_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serving(monkeypatch, {}, status=404)

    with pytest.raises(ContractConfigError, match="openapi.json"):
        not_deployed(ContractConfig(DEV_URL, DEV_KEY))


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


# ── The posted result ─────────────────────────────────────────────────────


def _result(*, forwarded: bool, reason: str) -> NotificationResult:
    return NotificationResult(forwarded=forwarded, event="notify", reason=reason)


def test_a_queued_result_reads_as_accepted() -> None:
    note = result_note(_result(forwarded=False, reason="queued"), "passed")

    assert note == "contract suite result queued for posting: passed"
    assert "not forwarded" not in note


def test_a_forwarded_result_reads_as_posted() -> None:
    note = result_note(_result(forwarded=True, reason="forwarded"), "passed")

    assert note == "contract suite result posted: passed"


def test_a_refused_result_reads_as_not_forwarded_with_its_reason() -> None:
    note = result_note(_result(forwarded=False, reason="delivery_failed"), "passed")

    assert note == "contract suite result not forwarded (delivery_failed): passed"


# ---------------------------------------------------------------------------
# The result message: which commit, and what started the run
# ---------------------------------------------------------------------------


def _version(
    monkeypatch: pytest.MonkeyPatch, payload: Any, status: int = 200
) -> list[str]:
    asked: list[str] = []

    def get(url: str, **_: Any) -> httpx.Response:
        asked.append(url)
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return asked


def test_the_commit_is_read_from_the_apis_version_when_none_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _version(monkeypatch, {"version": "2.91.0", "commit": "4cc4f8a9e1b2"})

    commit = deployed_commit("https://dev-api.kaianolevine.com/v1")

    assert asked == ["https://dev-api.kaianolevine.com/version"]
    message = result_message(
        "dev-api.kaianolevine.com",
        commit,
        Counter(passed=3),
        started_by("pull_request"),
    )
    assert message.startswith(
        "Contract suite passed against dev-api.kaianolevine.com at 4cc4f8a "
        "(promote pull request): 3 passed"
    )


def test_the_commit_is_unknown_only_when_version_gives_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _version(monkeypatch, {"version": "2.91.0", "commit": None})

    message = result_message(
        "dev-api.kaianolevine.com",
        deployed_commit("https://dev-api.kaianolevine.com"),
        Counter(failed=1),
        started_by("workflow_dispatch"),
    )

    assert "at an unknown commit (by hand)" in message
    assert message.startswith("Contract suite failed")


def test_a_failing_version_call_never_stops_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get(url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    assert deployed_commit("https://dev-api.kaianolevine.com") == ""
    _version(monkeypatch, {"commit": "abc"}, status=503)
    assert deployed_commit("https://dev-api.kaianolevine.com") == ""
    _version(monkeypatch, ["not", "an", "object"])
    assert deployed_commit("https://dev-api.kaianolevine.com") == ""


@pytest.mark.parametrize(
    ("event", "words"),
    [
        ("pull_request", "promote pull request"),
        ("repository_dispatch", "dev API deploy"),
        ("workflow_dispatch", BY_HAND),
        ("", BY_HAND),
    ],
)
def test_the_message_says_what_started_the_run(event: str, words: str) -> None:
    message = result_message("h", "4cc4f8a", Counter(passed=1), started_by(event))
    assert f"at 4cc4f8a ({words}):" in message
