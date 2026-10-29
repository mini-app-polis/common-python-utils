"""The catalog: every Kaiano API endpoint a cog or this library calls.

One entry per method and path. The typed client methods read their path from
here, and the contract suite's ledger iterates this tuple, so an endpoint
added here without a client method or a ledger entry fails a test rather than
going unnoticed.

The API's web-facing endpoints are deliberately absent. This is the contract
between the fleet and the API, not a description of the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from .agent_runs import (
    AgentResultAccepted,
    AgentRun,
    AgentRunCreate,
    AgentRunFailure,
    AgentRunResult,
    AgentSpend,
)
from .deejay import (
    CatalogLookupRequest,
    CatalogMatch,
    IngestResponseData,
    IngestSet,
    LivePlaysIngest,
    LivePlaysResponseData,
    SetListItem,
    SpotifyPlaylistsIngest,
    SpotifyPlaylistsIngestResponse,
)
from .evaluations import (
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    PipelineEvaluationWriteResult,
)
from .groomings import (
    GroomingBudgetSpentMarked,
    GroomingBudgetSpentRequest,
    GroomingClaimReleased,
    GroomingClaimRequest,
    GroomingClaimResult,
    GroomingJobAccepted,
    GroomingJobRequest,
    GroomingProposal,
    GroomingProposalAccepted,
    GroomingRecord,
    GroomingRecordWrite,
    GroomingReturnRate,
    GroomingRunCreate,
    GroomingRunRecorded,
    GroomingSpend,
)
from .notify import NotificationResult, NotifyRequest
from .runs import (
    DeejayRunAccepted,
    DeejayRunRequest,
    TranscriptionRunAccepted,
    TranscriptionRunRequest,
)
from .secret_rotations import (
    RotationEvent,
    RotationEventCreate,
    RotationIncident,
    RotationIncidentAccepted,
    RotationIncidentEntryStatus,
    RotationIncidentRegistration,
    RotationIncidentRequest,
    RotationIncidentTaskRequested,
    RotationRequest,
    RotationRequestAccepted,
    SecretChange,
)
from .tasks import TaskCreateAccepted, TaskCreateRequest
from .wcs import (
    WcsSourceCreate,
    WcsSourceItem,
    WcsTranscriptCreate,
    WcsTranscriptItem,
    WcsWikiExportItem,
)
from .wcs_wiki import (
    WcsWikiFacetsState,
    WcsWikiJobsAccepted,
    WcsWikiJobsRequest,
    WcsWikiLinksState,
    WcsWikiPagesWrite,
    WcsWikiPagesWriteResult,
    WcsWikiProseState,
    WcsWikiStateWriteResult,
    WcsWikiThinState,
    WcsWikiThinWrite,
    WcsWikiThinWriteResult,
)


@dataclass(frozen=True)
class Endpoint:
    """One route in the catalog.

    ``response`` is the type of the envelope's ``data``, not the envelope.
    ``scope`` is what the API requires of the caller; ``None`` means the
    route checks no scope. A ``{name}`` in ``path`` is filled from the
    client method's arguments.
    """

    name: str
    method: Literal["GET", "POST"]
    path: str
    request: type[BaseModel] | None
    response: Any
    scope: str | None


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="ingest",
        method="POST",
        path="/v1/ingest",
        request=IngestSet,
        response=IngestResponseData,
        scope="catalog.sets.write",
    ),
    # A read, under the scope that writes the catalog: the machine that
    # ingests a set's plays is the one that looks up where a file belongs.
    Endpoint(
        name="lookup_catalog",
        method="POST",
        path="/v1/catalog/lookup",
        request=CatalogLookupRequest,
        response=list[CatalogMatch | None],
        scope="catalog.sets.write",
    ),
    Endpoint(
        name="ingest_live_plays",
        method="POST",
        path="/v1/live-plays",
        request=LivePlaysIngest,
        response=LivePlaysResponseData,
        scope="catalog.plays.write",
    ),
    Endpoint(
        name="ingest_spotify_playlists",
        method="POST",
        path="/v1/spotify/playlists",
        request=SpotifyPlaylistsIngest,
        response=SpotifyPlaylistsIngestResponse,
        scope="catalog.sets.write",
    ),
    Endpoint(
        name="create_wcs_transcript",
        method="POST",
        path="/v1/wcs/transcripts",
        request=WcsTranscriptCreate,
        response=WcsTranscriptItem,
        scope="wcs.transcripts.write",
    ),
    Endpoint(
        name="create_wcs_source",
        method="POST",
        path="/v1/wcs/sources",
        request=WcsSourceCreate,
        response=WcsSourceItem,
        scope="wcs.sources.write",
    ),
    Endpoint(
        name="export_wcs_wiki",
        method="GET",
        path="/v1/wcs/wiki/export",
        request=None,
        response=WcsWikiExportItem,
        scope="wcs.corpus.read",
    ),
    # The curator's state. Read with the
    # corpus scope — the statements quote private sources — and written with
    # a scope of its own, so reading the corpus does not imply rewriting the
    # wiki.
    Endpoint(
        name="get_wcs_wiki_links",
        method="GET",
        path="/v1/wcs/wiki/state/links",
        request=None,
        response=WcsWikiLinksState,
        scope="wcs.corpus.read",
    ),
    Endpoint(
        name="put_wcs_wiki_links",
        method="POST",
        path="/v1/wcs/wiki/state/links",
        request=WcsWikiLinksState,
        response=WcsWikiStateWriteResult,
        scope="wcs.wiki.write",
    ),
    Endpoint(
        name="get_wcs_wiki_facets",
        method="GET",
        path="/v1/wcs/wiki/state/facets",
        request=None,
        response=WcsWikiFacetsState,
        scope="wcs.corpus.read",
    ),
    Endpoint(
        name="put_wcs_wiki_facets",
        method="POST",
        path="/v1/wcs/wiki/state/facets",
        request=WcsWikiFacetsState,
        response=WcsWikiStateWriteResult,
        scope="wcs.wiki.write",
    ),
    Endpoint(
        name="get_wcs_wiki_prose",
        method="GET",
        path="/v1/wcs/wiki/state/prose",
        request=None,
        response=WcsWikiProseState,
        scope="wcs.corpus.read",
    ),
    Endpoint(
        name="put_wcs_wiki_prose",
        method="POST",
        path="/v1/wcs/wiki/state/prose",
        request=WcsWikiProseState,
        response=WcsWikiStateWriteResult,
        scope="wcs.wiki.write",
    ),
    # The thin entities consolidation finds, for the admin's review in
    # /admin/wiki. The curator reads the list to know what to
    # remove, then writes what is thin now.
    Endpoint(
        name="get_wcs_wiki_thin",
        method="GET",
        path="/v1/wcs/wiki/state/thin",
        request=None,
        response=WcsWikiThinState,
        scope="wcs.corpus.read",
    ),
    Endpoint(
        name="put_wcs_wiki_thin",
        method="POST",
        path="/v1/wcs/wiki/state/thin",
        request=WcsWikiThinWrite,
        response=WcsWikiThinWriteResult,
        scope="wcs.wiki.write",
    ),
    # The page documents materialise builds; the API stores, filters and
    # serves them. Reading them is web-facing, so only the
    # write is here.
    Endpoint(
        name="put_wcs_wiki_pages",
        method="POST",
        path="/v1/wcs/wiki/pages",
        request=WcsWikiPagesWrite,
        response=WcsWikiPagesWriteResult,
        scope="wcs.wiki.write",
    ),
    # The follow-on jobs a wiki job names, put on the wiki-curator queue by the
    # API, which stays the fleet's one producer.
    Endpoint(
        name="enqueue_wcs_wiki_jobs",
        method="POST",
        path="/v1/wcs/wiki/jobs",
        request=WcsWikiJobsRequest,
        response=WcsWikiJobsAccepted,
        scope="wcs.wiki.write",
    ),
    Endpoint(
        name="request_deejay_run",
        method="POST",
        path="/v1/deejay/runs",
        request=DeejayRunRequest,
        response=DeejayRunAccepted,
        scope="deejay.runs.create",
    ),
    Endpoint(
        name="request_transcription_run",
        method="POST",
        path="/v1/transcription/runs",
        request=TranscriptionRunRequest,
        response=TranscriptionRunAccepted,
        scope="transcription.runs.create",
    ),
    Endpoint(
        name="request_task",
        method="POST",
        path="/v1/tasks",
        request=TaskCreateRequest,
        response=TaskCreateAccepted,
        scope="tasks.requests.create",
    ),
    Endpoint(
        name="list_groomings",
        method="GET",
        path="/v1/tasks/groomings",
        request=None,
        response=list[GroomingRecord],
        scope="tasks.groomings.read",
    ),
    Endpoint(
        name="record_grooming",
        method="POST",
        path="/v1/tasks/groomings",
        request=GroomingRecordWrite,
        response=GroomingRecord,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="record_grooming_run",
        method="POST",
        path="/v1/tasks/groomings/runs",
        request=GroomingRunCreate,
        response=GroomingRunRecorded,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="grooming_spend",
        method="GET",
        path="/v1/tasks/groomings/spend",
        request=None,
        response=GroomingSpend,
        scope="tasks.groomings.read",
    ),
    Endpoint(
        name="grooming_return_rate",
        method="GET",
        path="/v1/tasks/groomings/return-rate",
        request=None,
        response=GroomingReturnRate,
        scope="tasks.groomings.read",
    ),
    Endpoint(
        name="request_grooming",
        method="POST",
        path="/v1/tasks/groomings/jobs",
        request=GroomingJobRequest,
        response=GroomingJobAccepted,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="submit_grooming_proposal",
        method="POST",
        path="/v1/tasks/groomings/proposals",
        request=GroomingProposal,
        response=GroomingProposalAccepted,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="claim_grooming",
        method="POST",
        path="/v1/tasks/groomings/claims",
        request=GroomingClaimRequest,
        response=GroomingClaimResult,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="release_grooming_claim",
        method="POST",
        path="/v1/tasks/groomings/claims/release",
        request=GroomingClaimRequest,
        response=GroomingClaimReleased,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="mark_grooming_budget_spent",
        method="POST",
        path="/v1/tasks/groomings/budget-spent",
        request=GroomingBudgetSpentRequest,
        response=GroomingBudgetSpentMarked,
        scope="tasks.groomings.write",
    ),
    Endpoint(
        name="record_agent_run",
        method="POST",
        path="/v1/tasks/agent-runs",
        request=AgentRunCreate,
        response=AgentRun,
        scope="tasks.agent-runs.write",
    ),
    Endpoint(
        name="list_agent_runs",
        method="GET",
        path="/v1/tasks/agent-runs",
        request=None,
        response=list[AgentRun],
        scope="tasks.agent-runs.read",
    ),
    Endpoint(
        name="agent_spend",
        method="GET",
        path="/v1/tasks/agent-runs/spend",
        request=None,
        response=AgentSpend,
        scope="tasks.agent-runs.read",
    ),
    Endpoint(
        name="fail_agent_run",
        method="POST",
        path="/v1/tasks/agent-runs/failures",
        request=AgentRunFailure,
        response=AgentRun,
        scope="tasks.agent-runs.write",
    ),
    Endpoint(
        name="submit_agent_result",
        method="POST",
        path="/v1/tasks/agent-runs/results",
        request=AgentRunResult,
        response=AgentResultAccepted,
        scope="tasks.agent-runs.report",
    ),
    Endpoint(
        name="create_evaluation",
        method="POST",
        path="/v1/evaluations",
        request=PipelineEvaluationCreate,
        response=PipelineEvaluationWriteResult,
        scope="pipeline.evaluations.write",
    ),
    Endpoint(
        name="list_sets",
        method="GET",
        path="/v1/sets",
        request=None,
        response=list[SetListItem],
        scope=None,
    ),
    Endpoint(
        name="list_evaluations",
        method="GET",
        path="/v1/evaluations",
        request=None,
        response=list[PipelineEvaluationItem],
        scope=None,
    ),
    # The record of secret rotations. The rotator writes and
    # reads events; asking for a rotation has a scope of its own, so holding
    # the record's scopes does not let a caller start one.
    Endpoint(
        name="record_rotation_event",
        method="POST",
        path="/v1/secrets/rotation-events",
        request=RotationEventCreate,
        response=RotationEvent,
        scope="secrets.rotation-events.write",
    ),
    Endpoint(
        name="list_rotation_events",
        method="GET",
        path="/v1/secrets/rotation-events",
        request=None,
        response=list[RotationEvent],
        scope="secrets.rotation-events.read",
    ),
    Endpoint(
        name="request_rotation",
        method="POST",
        path="/v1/secrets/rotations",
        request=RotationRequest,
        response=RotationRequestAccepted,
        scope="secrets.rotations.create",
    ),
    # An incident: every secret rotated, compromised, under one id. Starting
    # one shares request_rotation's scope; the rotator registers and marks
    # entries and reads the incident with scopes of its own.
    Endpoint(
        name="start_rotation_incident",
        method="POST",
        path="/v1/secrets/rotation-incidents",
        request=RotationIncidentRequest,
        response=RotationIncidentAccepted,
        scope="secrets.rotations.create",
    ),
    Endpoint(
        name="register_rotation_incident",
        method="POST",
        path="/v1/secrets/rotation-incidents/{incident_id}/entries",
        request=RotationIncidentRegistration,
        response=RotationIncident,
        scope="secrets.rotation-incidents.write",
    ),
    Endpoint(
        name="mark_rotation_incident_task_requested",
        method="POST",
        path="/v1/secrets/rotation-incidents/{incident_id}/task-requested",
        request=RotationIncidentTaskRequested,
        response=RotationIncidentEntryStatus,
        scope="secrets.rotation-incidents.write",
    ),
    Endpoint(
        name="get_rotation_incident",
        method="GET",
        path="/v1/secrets/rotation-incidents/{incident_id}",
        request=None,
        response=RotationIncident,
        scope="secrets.rotation-incidents.read",
    ),
    Endpoint(
        name="list_secret_changes",
        method="GET",
        path="/v1/secrets/changes",
        request=None,
        response=list[SecretChange],
        scope="secrets.changes.read",
    ),
    Endpoint(
        name="send_notification",
        method="POST",
        path="/v1/notify",
        request=NotifyRequest,
        response=NotificationResult,
        scope="notify.messages.send",
    ),
)

ENDPOINTS_BY_NAME: dict[str, Endpoint] = {e.name: e for e in ENDPOINTS}
