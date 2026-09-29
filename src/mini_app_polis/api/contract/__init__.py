"""Typed contract for the Kaiano API endpoints the fleet calls.

The request and response models here are the ones the API validates against:
api-kaianolevine-com imports them for these routes rather than defining its
own. A cog that builds a request from these models sends what the API
accepts, and the contract suite in this repo checks the deployed API still
answers in these shapes.

Only endpoints a cog or this library calls are here. See ``ENDPOINTS``.
"""

from __future__ import annotations

from .deejay import (
    IngestResponseData,
    IngestSet,
    IngestTrack,
    LivePlayIngest,
    LivePlaysIngest,
    LivePlaysResponseData,
    SpotifyPlaylistIngest,
    SpotifyPlaylistsIngest,
    SpotifyPlaylistsIngestResponse,
)
from .endpoints import (
    ENDPOINTS,
    ENDPOINTS_BY_NAME,
    Endpoint,
)
from .envelope import (
    Envelope,
    ErrorDetail,
    ErrorEnvelope,
    Meta,
)
from .evaluations import (
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    PipelineEvaluationWriteResult,
)
from .notify import (
    NotificationResult,
    NotifyRequest,
)
from .runs import (
    DeejayRunAccepted,
    DeejayRunRequest,
    DriveFileRef,
    TranscriptionRunAccepted,
    TranscriptionRunRequest,
)
from .wcs import (
    WcsDrillPurposeItem,
    WcsEntityDefinitionItem,
    WcsEntityItem,
    WcsEntityRelationItem,
    WcsExtractionCommonMistake,
    WcsExtractionCompetitionNote,
    WcsExtractionDrillPurpose,
    WcsExtractionEntity,
    WcsExtractionEntityDefinition,
    WcsExtractionEntityRelation,
    WcsExtractionRawOutput,
    WcsExtractionReference,
    WcsExtractionTechniqueRequirement,
    WcsInstructorItem,
    WcsSourceAttributionItem,
    WcsSourceCreate,
    WcsSourceItem,
    WcsSourceReferenceItem,
    WcsSourceType,
    WcsTechniqueRequirementItem,
    WcsTranscriptCreate,
    WcsTranscriptItem,
    WcsWikiExportItem,
)

__all__ = [
    "DeejayRunAccepted",
    "DeejayRunRequest",
    "DriveFileRef",
    "ENDPOINTS",
    "ENDPOINTS_BY_NAME",
    "Endpoint",
    "Envelope",
    "ErrorDetail",
    "ErrorEnvelope",
    "IngestResponseData",
    "IngestSet",
    "IngestTrack",
    "LivePlayIngest",
    "LivePlaysIngest",
    "LivePlaysResponseData",
    "Meta",
    "NotificationResult",
    "NotifyRequest",
    "PipelineEvaluationCreate",
    "PipelineEvaluationItem",
    "PipelineEvaluationWriteResult",
    "SpotifyPlaylistIngest",
    "SpotifyPlaylistsIngest",
    "SpotifyPlaylistsIngestResponse",
    "TranscriptionRunAccepted",
    "TranscriptionRunRequest",
    "WcsDrillPurposeItem",
    "WcsEntityDefinitionItem",
    "WcsEntityItem",
    "WcsEntityRelationItem",
    "WcsExtractionCommonMistake",
    "WcsExtractionCompetitionNote",
    "WcsExtractionDrillPurpose",
    "WcsExtractionEntity",
    "WcsExtractionEntityDefinition",
    "WcsExtractionEntityRelation",
    "WcsExtractionRawOutput",
    "WcsExtractionReference",
    "WcsExtractionTechniqueRequirement",
    "WcsInstructorItem",
    "WcsSourceAttributionItem",
    "WcsSourceCreate",
    "WcsSourceItem",
    "WcsSourceReferenceItem",
    "WcsSourceType",
    "WcsTechniqueRequirementItem",
    "WcsTranscriptCreate",
    "WcsTranscriptItem",
    "WcsWikiExportItem",
]
