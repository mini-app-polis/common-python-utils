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

from .deejay import (
    IngestResponseData,
    IngestSet,
    LivePlaysIngest,
    LivePlaysResponseData,
    SpotifyPlaylistsIngest,
    SpotifyPlaylistsIngestResponse,
)
from .evaluations import (
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    PipelineEvaluationWriteResult,
)
from .notify import NotificationResult, NotifyRequest
from .runs import (
    DeejayRunAccepted,
    DeejayRunRequest,
    TranscriptionRunAccepted,
    TranscriptionRunRequest,
)
from .wcs import (
    WcsSourceCreate,
    WcsSourceItem,
    WcsTranscriptCreate,
    WcsTranscriptItem,
    WcsWikiExportItem,
)


@dataclass(frozen=True)
class Endpoint:
    """One route in the catalog.

    ``response`` is the type of the envelope's ``data``, not the envelope.
    ``scope`` is what the API requires of the caller; ``None`` means the
    route checks no scope. ``callers`` names who calls it today, so a change
    here says who has to move.
    """

    name: str
    method: Literal["GET", "POST"]
    path: str
    request: type[BaseModel] | None
    response: Any
    scope: str | None
    callers: tuple[str, ...]


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="ingest",
        method="POST",
        path="/v1/ingest",
        request=IngestSet,
        response=IngestResponseData,
        scope="catalog.sets.write",
        callers=("deejay-cog",),
    ),
    Endpoint(
        name="ingest_live_plays",
        method="POST",
        path="/v1/live-plays",
        request=LivePlaysIngest,
        response=LivePlaysResponseData,
        scope="catalog.plays.write",
        callers=("deejay-cog",),
    ),
    Endpoint(
        name="ingest_spotify_playlists",
        method="POST",
        path="/v1/spotify/playlists",
        request=SpotifyPlaylistsIngest,
        response=SpotifyPlaylistsIngestResponse,
        scope="catalog.sets.write",
        callers=("deejay-cog",),
    ),
    Endpoint(
        name="create_wcs_transcript",
        method="POST",
        path="/v1/wcs/transcripts",
        request=WcsTranscriptCreate,
        response=WcsTranscriptItem,
        scope="wcs.transcripts.write",
        callers=("transcription-cog",),
    ),
    Endpoint(
        name="create_wcs_source",
        method="POST",
        path="/v1/wcs/sources",
        request=WcsSourceCreate,
        response=WcsSourceItem,
        scope="wcs.sources.write",
        callers=("transcription-cog",),
    ),
    Endpoint(
        name="export_wcs_wiki",
        method="GET",
        path="/v1/wcs/wiki/export",
        request=None,
        response=WcsWikiExportItem,
        scope="wcs.corpus.read",
        callers=("wiki-curator-cog",),
    ),
    Endpoint(
        name="request_deejay_run",
        method="POST",
        path="/v1/deejay/runs",
        request=DeejayRunRequest,
        response=DeejayRunAccepted,
        scope="deejay.runs.create",
        callers=("watcher-cog",),
    ),
    Endpoint(
        name="request_transcription_run",
        method="POST",
        path="/v1/transcription/runs",
        request=TranscriptionRunRequest,
        response=TranscriptionRunAccepted,
        scope="transcription.runs.create",
        callers=("watcher-cog",),
    ),
    Endpoint(
        name="create_evaluation",
        method="POST",
        path="/v1/evaluations",
        request=PipelineEvaluationCreate,
        response=PipelineEvaluationWriteResult,
        scope="pipeline.evaluations.write",
        callers=("evaluator-cog", "wiki-curator-cog", "common-python-utils"),
    ),
    Endpoint(
        name="list_evaluations",
        method="GET",
        path="/v1/evaluations",
        request=None,
        response=list[PipelineEvaluationItem],
        scope=None,
        callers=("evaluator-cog",),
    ),
    Endpoint(
        name="send_notification",
        method="POST",
        path="/v1/notify",
        request=NotifyRequest,
        response=NotificationResult,
        scope="notify.messages.send",
        callers=("common-python-utils",),
    ),
)

ENDPOINTS_BY_NAME: dict[str, Endpoint] = {e.name: e for e in ENDPOINTS}
