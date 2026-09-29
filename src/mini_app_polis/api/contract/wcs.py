"""Contract for the WCS corpus endpoints the cogs call.

``POST /v1/wcs/transcripts`` and ``POST /v1/wcs/sources`` (transcription-cog),
and ``GET /v1/wcs/wiki/export`` (wiki-curator-cog). The WCS read endpoints the
web front ends use stay in the API; only what a cog sends or receives is here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WcsSourceType = Literal[
    "plaud",
    "otter",
    "zoom",
    "google_meet",
    "manual",
    "unknown",
]


class WcsTranscriptCreate(BaseModel):
    """POST /v1/wcs/transcripts — called by transcription-cog."""

    raw_text: str = Field(..., description="Semantic value for raw text.")
    source_type: WcsSourceType = Field(
        default="unknown", description="Semantic value for source type."
    )
    source_filename: str = Field(..., description="Semantic value for source filename.")
    drive_file_id: str = Field(..., description="Semantic value for drive file id.")

    model_config = ConfigDict(extra="forbid")


class WcsTranscriptItem(BaseModel):
    """Stored WCS transcript metadata returned by API routes."""

    id: uuid.UUID = Field(..., description="Unique identifier for this wcstranscript.")
    source_type: str = Field(..., description="Semantic value for source type.")
    source_filename: str = Field(..., description="Semantic value for source filename.")
    drive_file_id: str = Field(..., description="Semantic value for drive file id.")
    created_at: dt.datetime = Field(
        ..., description="Timestamp when this record was created."
    )


class WcsExtractionEntity(BaseModel):
    """One entity claim extracted from a source."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["concept", "technique", "pattern", "drill"] = Field(
        ...,
        description="Discriminator for the entity kind (concept, technique, pattern, drill).",
    )
    name: str = Field(min_length=1, description="Human-readable name.")
    prose: str = Field("", description="Free-text content for this row.")
    external_origin: dict | None = Field(
        None, description="Optional external attribution (book, video, etc.)."
    )


class WcsExtractionEntityDefinition(BaseModel):
    """Per-source vocabulary definition from extraction."""

    model_config = ConfigDict(extra="ignore")

    entity_name: str = Field(
        min_length=1, description="Display name of the WCS entity."
    )
    definition: str = Field(
        min_length=1,
        description="Definition prose attached to an entity for one source.",
    )


class WcsExtractionEntityRelation(BaseModel):
    """Cross-entity relation from extraction."""

    model_config = ConfigDict(extra="ignore")

    from_: str = Field(alias="from", min_length=1, description="From.")
    to: str = Field(min_length=1, description="To.")
    relation_kind: str = Field(
        min_length=1,
        description="Discriminator for the entity-to-entity relation type.",
    )
    prose: str = Field("", description="Free-text content for this row.")


class WcsExtractionDrillPurpose(BaseModel):
    """Drill purpose from extraction."""

    model_config = ConfigDict(extra="ignore")

    drill_name: str = Field(min_length=1, description="Drill name.")
    skill_description: str = Field(
        min_length=1, description="Free-text description of the skill."
    )
    focus_context: str = Field(
        "", description="Focus or context hint that scopes how this row applies."
    )


class WcsExtractionTechniqueRequirement(BaseModel):
    """Technique requirement from extraction."""

    model_config = ConfigDict(extra="ignore")

    technique_name: str = Field(min_length=1, description="Technique name.")
    skill_description: str = Field(
        min_length=1, description="Free-text description of the skill."
    )


class WcsExtractionCommonMistake(BaseModel):
    """Common mistake from extraction."""

    model_config = ConfigDict(extra="ignore")

    entity_name: str | None = Field(None, description="Display name of the WCS entity.")
    mistake: str = Field(min_length=1, description="Mistake.")
    correction: str = Field(min_length=1, description="Correction.")


class WcsExtractionCompetitionNote(BaseModel):
    """Competition note from extraction."""

    model_config = ConfigDict(extra="ignore")

    note: str = Field(min_length=1, description="Note.")
    entity_name: str | None = Field(None, description="Display name of the WCS entity.")
    context: str = Field("", description="Free-text context for the reference.")


class WcsExtractionReference(BaseModel):
    """Person reference from extraction."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, description="Human-readable name.")
    type: (
        Literal[
            "instructor",
            "teacher",
            "dancer",
            "judge",
            "competitor",
            "coach",
            "pro",
        ]
        | None
    ) = Field(None, description="Type.")
    context: str = Field("", description="Free-text context for the reference.")


class WcsExtractionRawOutput(BaseModel):
    """The full extraction payload produced by transcription-cog's prompt.

    Matches EXTRACTION_SCHEMA in transcription_cog/schema.py.
    """

    model_config = ConfigDict(extra="allow")  # forward-compatible

    title: str = Field("", description="Topic or display title.")
    summary: str = Field("", description="Summary.")
    entities: list[WcsExtractionEntity] = Field(
        default_factory=list, description="WCS entity rows attached to this response."
    )
    entity_definitions: list[WcsExtractionEntityDefinition] = Field(
        default_factory=list,
        description="Entity definitions.",
    )
    entity_relations: list[WcsExtractionEntityRelation] = Field(
        default_factory=list, description="Entity relations."
    )
    drill_purposes: list[WcsExtractionDrillPurpose] = Field(
        default_factory=list,
        description="Drill-to-purpose links sourced from this row.",
    )
    technique_requirements: list[WcsExtractionTechniqueRequirement] = Field(
        default_factory=list,
        description="Technique-to-requirement links sourced from this row.",
    )
    common_mistakes: list[WcsExtractionCommonMistake] = Field(
        default_factory=list, description="Common mistakes."
    )
    competition_notes: list[WcsExtractionCompetitionNote] = Field(
        default_factory=list, description="Competition notes."
    )
    student_observations: list[dict] = Field(
        default_factory=list, description="Student observations."
    )
    action_items: list[dict] = Field(default_factory=list, description="Action items.")
    quotes: list[dict] = Field(default_factory=list, description="Quotes.")
    references: list[WcsExtractionReference] = Field(
        default_factory=list,
        description="Bare-reference rows (instructors mentioned but not attributed).",
    )
    off_topic_notes: list[dict] = Field(
        default_factory=list, description="Off topic notes."
    )
    suggested_new_sections: list[dict] = Field(
        default_factory=list, description="Suggested new sections."
    )


class WcsSourceCreate(BaseModel):
    """Payload for POST /v1/wcs/sources."""

    transcript_id: uuid.UUID = Field(
        ..., description="Identifier of the upstream transcript."
    )
    title: str | None = Field(None, description="Topic or display title.")
    session_date: dt.date | None = Field(
        None, description="Calendar date of the lesson session."
    )
    session_type: str = Field(
        "other", description="Session type — e.g. private_lesson, group_class, other."
    )
    instructors_raw: list[str] = Field(
        default_factory=list,
        description="Verbatim upstream instructor names before alias resolution.",
    )
    students_raw: list[str] = Field(
        default_factory=list,
        description="Verbatim upstream student names before alias resolution.",
    )
    organization: str = Field(
        "", description="Organization, studio, or event context for the session."
    )
    visibility: str = Field(
        "private", description="Coarse access-control flag (private vs. public)."
    )
    is_default_visible: bool = Field(
        False, description="Whether the source is shown in the default catalog."
    )
    extractor_version: str = Field(..., description="Extractor version.")
    extractor_model: str = Field(..., description="Extractor model.")
    extractor_provider: str = Field(..., description="Extractor provider.")
    prompt_version: str = Field(..., description="Prompt version.")
    raw_output: WcsExtractionRawOutput = Field(
        ..., description="Raw upstream extraction payload, unparsed."
    )


class WcsEntityItem(BaseModel):
    """Canonical entity returned by wiki/read endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    slug: str = Field(
        ..., description="Lowercase, hyphen-separated canonical identifier."
    )
    canonical_name: str = Field(
        ..., description="Canonical, post-collapse display name."
    )
    kind: str = Field(
        ...,
        description="Discriminator for the entity kind (concept, technique, pattern, drill).",
    )
    overview_md: str = Field(..., description="Overview md.")
    status: str = Field(
        ..., description="Lifecycle status flag (e.g., stub, draft, mature)."
    )
    external_origin: dict = Field(
        ..., description="Optional external attribution (book, video, etc.)."
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Variant names that resolve to this canonical row.",
    )


class WcsSourceAttributionItem(BaseModel):
    """Source attribution row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    source_id: uuid.UUID = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    entity_id: uuid.UUID = Field(
        ..., description="Identifier of the WCS entity this attribution is about."
    )
    instructor_id: uuid.UUID | None = Field(
        ..., description="Identifier of the WCS instructor."
    )
    attribution_kind: str = Field(
        ..., description="Discriminator for the attribution row type."
    )
    prose: str = Field(..., description="Free-text content for this row.")
    raw_term: str = Field(
        ..., description="Raw term string as it appeared in the upstream extraction."
    )
    position: int = Field(
        ..., description="Ordinal position of the row within its source."
    )
    drill_goal: str | None = Field(None, description="Drill goal.")
    drill_steps: list[str] | None = Field(None, description="Drill steps.")
    mistake_text: str | None = Field(None, description="Mistake text.")
    correction_text: str | None = Field(None, description="Correction text.")
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    entity_slug: str = Field("", description="Canonical slug of the attributed entity.")
    entity_name: str = Field(
        "", description="Canonical display name of the attributed entity."
    )
    entity_kind: str = Field(
        "",
        description="Kind of the attributed entity (concept|technique|pattern|drill).",
    )
    instructor_slug: str | None = Field(
        None, description="Canonical slug of the attributing instructor, if linked."
    )
    instructor_name: str | None = Field(
        None,
        description="Canonical display name of the attributing instructor, if linked.",
    )


class WcsEntityRelationItem(BaseModel):
    """Entity relation row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    from_entity_id: uuid.UUID = Field(..., description="From entity id.")
    to_entity_id: uuid.UUID = Field(..., description="To entity id.")
    relation_kind: str = Field(
        ..., description="Discriminator for the entity-to-entity relation type."
    )
    source_id: uuid.UUID | None = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    prose: str = Field(..., description="Free-text content for this row.")
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    from_entity_slug: str = Field("", description="Canonical slug of the from-entity.")
    from_entity_name: str = Field(
        "", description="Canonical display name of the from-entity."
    )
    from_entity_kind: str = Field("", description="Kind of the from-entity.")
    to_entity_slug: str = Field("", description="Canonical slug of the to-entity.")
    to_entity_name: str = Field(
        "", description="Canonical display name of the to-entity."
    )
    to_entity_kind: str = Field("", description="Kind of the to-entity.")


class WcsDrillPurposeItem(BaseModel):
    """Drill purpose row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    drill_entity_id: uuid.UUID = Field(..., description="Drill entity id.")
    source_id: uuid.UUID | None = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    skill_name: str = Field(
        ..., description="Human-readable skill name this row references."
    )
    skill_slug: str = Field(..., description="Skill slug.")
    prose: str = Field(..., description="Free-text content for this row.")
    focus_context: str = Field(
        ..., description="Focus or context hint that scopes how this row applies."
    )
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    drill_entity_slug: str = Field(
        "", description="Canonical slug of the drill entity."
    )
    drill_entity_name: str = Field(
        "", description="Canonical display name of the drill entity."
    )


class WcsTechniqueRequirementItem(BaseModel):
    """Technique requirement row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    technique_entity_id: uuid.UUID = Field(..., description="Technique entity id.")
    source_id: uuid.UUID | None = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    skill_name: str = Field(
        ..., description="Human-readable skill name this row references."
    )
    skill_slug: str = Field(..., description="Skill slug.")
    prose: str = Field(..., description="Free-text content for this row.")
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    technique_entity_slug: str = Field(
        "", description="Canonical slug of the technique entity."
    )
    technique_entity_name: str = Field(
        "", description="Canonical display name of the technique entity."
    )


class WcsEntityDefinitionItem(BaseModel):
    """Entity definition row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    entity_id: uuid.UUID = Field(..., description="Identifier of the WCS entity.")
    source_id: uuid.UUID = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    instructor_id: uuid.UUID | None = Field(
        ..., description="Identifier of the WCS instructor."
    )
    term: str = Field(..., description="Term.")
    definition: str = Field(
        ..., description="Definition prose attached to an entity for one source."
    )
    position: int = Field(
        ..., description="Ordinal position of the row within its source."
    )
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    entity_slug: str = Field("", description="Canonical slug of the defined entity.")
    entity_name: str = Field(
        "", description="Canonical display name of the defined entity."
    )
    entity_kind: str = Field("", description="Kind of the defined entity.")
    instructor_slug: str | None = Field(
        None, description="Canonical slug of the defining instructor, if linked."
    )
    instructor_name: str | None = Field(
        None,
        description="Canonical display name of the defining instructor, if linked.",
    )


class WcsInstructorItem(BaseModel):
    """Instructor row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    slug: str = Field(
        ..., description="Lowercase, hyphen-separated canonical identifier."
    )
    canonical_name: str = Field(
        ..., description="Canonical, post-collapse display name."
    )
    background_md: str = Field(..., description="Background md.")
    teaching_themes_md: str = Field(..., description="Teaching themes md.")
    notable_framings_md: str = Field(..., description="Notable framings md.")
    aliases: list[str] = Field(
        default_factory=list,
        description="Variant names that resolve to this canonical row.",
    )


class WcsSourceItem(BaseModel):
    """Source row for API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    transcript_id: uuid.UUID = Field(
        ..., description="Identifier of the upstream transcript."
    )
    title: str | None = Field(..., description="Topic or display title.")
    session_date: dt.date | None = Field(
        ..., description="Calendar date of the lesson session."
    )
    session_type: str = Field(
        ..., description="Session type — e.g. private_lesson, group_class, other."
    )
    instructors_raw: list[str] = Field(
        ..., description="Verbatim upstream instructor names before alias resolution."
    )
    students_raw: list[str] = Field(
        ..., description="Verbatim upstream student names before alias resolution."
    )
    organization: str = Field(
        ..., description="Organization, studio, or event context for the session."
    )
    visibility: str = Field(
        ..., description="Coarse access-control flag (private vs. public)."
    )
    is_default_visible: bool = Field(
        ..., description="Whether the source is shown in the default catalog."
    )
    created_at: dt.datetime = Field(..., description="Timestamp this row was created.")


class WcsSourceReferenceItem(BaseModel):
    """A person mentioned in a source. Stored as a raw name with context;
    not linked to canonical instructor records."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique identifier.")
    source_id: uuid.UUID = Field(
        ..., description="Identifier of the WCS source this row belongs to."
    )
    referenced_name: str = Field(
        ..., description="Raw mention name from the extraction."
    )
    context: str = Field(..., description="Free-text context for the reference.")
    ref_type: str = Field(..., description="Ref type.")
    origin: str = Field(
        ..., description="Originating source or upstream attribution metadata."
    )
    created_at: dt.datetime = Field(
        ..., description="Timestamp when this record was created."
    )


class WcsWikiExportItem(BaseModel):
    """Bulk corpus export for wiki-curator-cog."""

    entities: list[WcsEntityItem] = Field(
        ..., description="WCS entity rows attached to this response."
    )
    instructors: list[WcsInstructorItem] = Field(
        ..., description="Instructor rows attached to this response."
    )
    sources: list[WcsSourceItem] = Field(
        ..., description="Source rows attached to this response."
    )
    attributions: list[WcsSourceAttributionItem] = Field(
        ..., description="Attributions sourced from this row's parent record."
    )
    definitions: list[WcsEntityDefinitionItem] = Field(
        ..., description="Definitions sourced from this row's parent record."
    )
    relations: list[WcsEntityRelationItem] = Field(
        ..., description="Entity-to-entity relations sourced from this row."
    )
    drill_purposes: list[WcsDrillPurposeItem] = Field(
        ..., description="Drill-to-purpose links sourced from this row."
    )
    technique_requirements: list[WcsTechniqueRequirementItem] = Field(
        ..., description="Technique-to-requirement links sourced from this row."
    )
    references: list[WcsSourceReferenceItem] = Field(
        ...,
        description="People mentioned in sources (raw names, not attributed).",
    )
    exported_at: dt.datetime = Field(..., description="Exported at.")
