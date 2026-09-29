"""Live contract suite: the fleet's endpoint catalog, checked against the
deployed development API.

The unit tests in tests/ prove the client sends and parses what the catalog
describes. This suite proves the API still answers that way. Every catalog
endpoint is called through the same typed method a cog uses, so a response
the API renames, drops, retypes or makes nullable fails validation here
before a cog meets it. It also pins what a schema cannot see: which
endpoints refuse an unauthenticated caller, in the error envelope, and the
deduplication the cogs' retries rely on.

Writes are real and fixed: each payload uses the same identifiers every run,
and each endpoint upserts or deduplicates on them. See harness.py for that,
and for the guards that keep the suite off production.

Run: uv run pytest contract --no-cov
Needs CONTRACT_API_URL and CONTRACT_SUITE_API_KEY. Tests run in file order,
and the ledger check is last, so run the whole module rather than a subset.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator

import httpx
import pytest

from mini_app_polis.api.contract import (
    ENDPOINTS,
    Envelope,
    ErrorEnvelope,
    IngestSet,
    IngestTrack,
    LivePlayIngest,
    LivePlaysIngest,
    NotifyRequest,
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    SpotifyPlaylistIngest,
    SpotifyPlaylistsIngest,
    WcsTranscriptCreate,
)

from .harness import (
    MACHINE_NAME,
    NOT_EXERCISED,
    ContractConfig,
    ContractConfigError,
    Ledger,
    LedgerClient,
    load_config,
    preflight,
)

#: Every fixture is named with this, so anything the suite wrote is findable.
FIXTURE = "contract-suite-fixture"

#: Far enough back that nothing the suite writes surfaces in a "recent" view.
FIXTURE_DATE = dt.date(2000, 1, 1)


@pytest.fixture(scope="module")
def config() -> ContractConfig:
    try:
        return load_config()
    except ContractConfigError as exc:
        pytest.exit(str(exc), returncode=2)


@pytest.fixture(scope="module")
def ledger() -> Ledger:
    ledger = Ledger()
    for name, reason in NOT_EXERCISED.items():
        ledger.skip(name, reason)
    return ledger


@pytest.fixture(scope="module")
def api(config: ContractConfig, ledger: Ledger) -> Iterator[LedgerClient]:
    client = LedgerClient(config, ledger)
    try:
        preflight(client)
    except ContractConfigError as exc:
        pytest.exit(str(exc), returncode=2)
    yield client


# ── Access rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [e for e in ENDPOINTS if e.scope is not None],
    ids=lambda e: e.name,
)
def test_refuses_an_unauthenticated_caller_in_the_error_envelope(
    config: ContractConfig, api: LedgerClient, endpoint
) -> None:
    """A scoped endpoint answers 401, in the envelope, before reading the body.

    The empty body is deliberate: authentication runs before validation, so
    nothing here reaches a handler, and the run endpoints are safe to probe
    even though development has no queues.
    """
    response = httpx.request(
        endpoint.method,
        f"{config.api_url}{endpoint.path}",
        json={} if endpoint.method == "POST" else None,
        timeout=30.0,
    )

    assert response.status_code == 401, response.text
    ErrorEnvelope.model_validate(response.json())


def test_lists_evaluations_without_authentication(
    config: ContractConfig, api: LedgerClient
) -> None:
    """GET /v1/evaluations is public; evaluator-cog relies on nothing more."""
    response = httpx.get(
        f"{config.api_url}/v1/evaluations", params={"limit": 1}, timeout=30.0
    )

    assert response.status_code == 200, response.text
    Envelope[list[PipelineEvaluationItem]].model_validate(response.json())


# ── deejay-cog ────────────────────────────────────────────────────────────


def test_ingest_is_idempotent_on_source_file(api: LedgerClient) -> None:
    payload = IngestSet(
        set_date=FIXTURE_DATE,
        venue="Contract Suite",
        source_file=f"{FIXTURE}.csv",
        tracks=[IngestTrack(play_order=1, title=FIXTURE, artist="Contract Suite")],
    )

    api.ingest(payload)
    again = api.ingest(payload)

    assert again.tracks_created == 0


def test_live_plays_skip_a_play_already_recorded(api: LedgerClient) -> None:
    payload = LivePlaysIngest(
        plays=[
            LivePlayIngest(
                played_at=dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
                title=FIXTURE,
                artist="Contract Suite",
            )
        ]
    )

    api.ingest_live_plays(payload)
    again = api.ingest_live_plays(payload)

    assert (again.inserted, again.skipped) == (0, 1)


def test_spotify_playlists_leave_an_unchanged_snapshot_alone(
    api: LedgerClient,
) -> None:
    # A synthetic id: this endpoint upserts by playlist id across owners,
    # so a real one would overwrite a real row.
    payload = SpotifyPlaylistsIngest(
        playlists=[
            SpotifyPlaylistIngest(
                id=FIXTURE,
                name="Contract Suite fixture",
                url=f"https://open.spotify.com/playlist/{FIXTURE}",
                uri=f"spotify:playlist:{FIXTURE}",
                snapshot_id=f"{FIXTURE}-1",
                owner_id=MACHINE_NAME,
            )
        ]
    )

    api.ingest_spotify_playlists(payload)
    again = api.ingest_spotify_playlists(payload)

    assert (again.upserted, again.unchanged) == (0, 1)


# ── transcription-cog ─────────────────────────────────────────────────────


def test_transcripts_upsert_on_drive_file_id(api: LedgerClient) -> None:
    # No source is ever created from this transcript (see NOT_EXERCISED), so
    # re-posting it has nothing to demote.
    payload = WcsTranscriptCreate(
        raw_text="Contract suite fixture transcript.",
        source_filename=f"{FIXTURE}.txt",
        drive_file_id=FIXTURE,
    )

    first = api.create_wcs_transcript(payload)
    again = api.create_wcs_transcript(payload)

    assert again.id == first.id


# ── wiki-curator-cog ──────────────────────────────────────────────────────


def test_exports_the_wiki(api: LedgerClient) -> None:
    api.export_wcs_wiki()


# ── evaluator-cog, wiki-curator-cog, pipeline_status ──────────────────────


def test_evaluations_deduplicate_and_list_back(api: LedgerClient) -> None:
    # A repo and dimension of its own, so the fixture never mixes into a
    # real repository's latest findings or a real dimension's totals.
    payload = PipelineEvaluationCreate(
        run_id=FIXTURE,
        repo=MACHINE_NAME,
        dimension="contract_suite",
        severity="INFO",
        finding="Contract suite fixture finding; deduplicated after the first run.",
        source=MACHINE_NAME,
    )

    api.create_evaluation(payload)
    again = api.create_evaluation(payload)
    listed = api.list_evaluations(repo=MACHINE_NAME, run_id=FIXTURE, limit=10)

    assert again.deduplicated is True
    assert again.id in {item.id for item in listed}


# ── every cog, through pipeline_status ────────────────────────────────────


def test_sends_a_notification(config: ContractConfig, api: LedgerClient) -> None:
    api_sha = os.environ.get("CONTRACT_API_SHA", "").strip()
    against = f"{config.host} at {api_sha[:7]}" if api_sha else config.host

    result = api.send_notification(
        NotifyRequest(
            content=f"Contract suite ran against {against}.",
            username=MACHINE_NAME,
        )
    )

    assert result.forwarded is True, result.reason


# ── The ledger. Last, so it sees every call above. ───────────────────────


def test_every_catalog_endpoint_is_exercised_or_explained(ledger: Ledger) -> None:
    report = ledger.report()

    print("\nexercised:", ", ".join(report.exercised))
    for name, reason in report.skipped:
        print(f"not exercised: {name} — {reason}")

    assert report.unaccounted == [], (
        "catalog endpoints neither exercised nor listed in NOT_EXERCISED: "
        f"{report.unaccounted}"
    )
