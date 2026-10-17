"""Contract for the wiki curator's state: references, facet decisions, prose.

wiki-curator-cog's three passes — linking, consolidation and generation —
each keep what they decided, and a watermark saying under which prompt
version they decided it, so a re-run pays only for what is new. The state
lived in three JSON files (``links.json``, ``facets.json``, ``prose.json``);
the API now holds it, and these documents are those files' shapes, field for
field. Each is keyed exactly as its file is: a statement key
(``attribution:<uuid>``) or an entity slug.

``GET /v1/wcs/wiki/state/{links,facets,prose}`` returns a whole document.
``POST`` to the same path upserts the entries it carries — each one replaced
whole — and records the document's version and model; entries it does not
carry are left alone. A re-post is a no-op, so an import can be repeated.

Like every response model in this catalog, these tolerate fields they do
not know, so a cog on an older contract keeps working. That means a field
the store does not hold would be dropped in transit rather than refused; the
curator's import checks for that before it sends anything, and its verify
step compares the files it sent with what the API returns.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

# ── Linking: links.json ──


class WcsWikiReference(BaseModel):
    """One concept a statement refers to, or denies referring to."""

    name: str = Field(..., description="The concept as the model named it.")
    target_slug: str = Field(..., description="The concept's slug.")
    target_entity_id: uuid.UUID | None = Field(
        None,
        description=(
            "The entity, when the concept has one; null for a concept the "
            "linker proposed, which lives in the wiki state only."
        ),
    )
    polarity: Literal["assert", "deny"] = Field(
        ..., description="Whether the statement asserts or denies the reference."
    )


class WcsWikiLinkedStatement(BaseModel):
    """One statement the linker has read, and what it refers to."""

    linker_version: str = Field(..., description="Prompt version it was linked under.")
    model: str = Field("", description="Model that linked it.")
    subject_slug: str = Field(..., description="The entity the statement is about.")
    source_id: uuid.UUID | None = Field(None, description="Source it came from.")
    session_date: dt.date | None = Field(None, description="The source's date.")
    text: str = Field(..., description="The statement as the linker read it.")
    references: list[WcsWikiReference] = Field(
        default_factory=list, description="References, in the order found."
    )


class WcsWikiLinksState(BaseModel):
    """``links.json``: every linked statement, by statement key."""

    linker_version: str = Field(..., description="Linker version of the last run.")
    model: str = Field("", description="Model of the last run.")
    statements: dict[str, WcsWikiLinkedStatement] = Field(
        default_factory=dict, description="Statement key → its references."
    )


# ── Consolidation: facets.json ──


class WcsWikiFacetDecision(BaseModel):
    """Whether one entity folds into a broader one, and why."""

    parent: str | None = Field(None, description="Parent slug; null for standalone.")
    same: bool = Field(False, description="Folded as another name, not a facet.")
    reason: str = Field("", description="The model's stated reason.")
    version: str = Field(..., description="Its group's consolidator version.")
    model: str = Field("", description="Model that decided it.")
    decided_at: dt.date | None = Field(None, description="When it was decided.")


class WcsWikiFacetsState(BaseModel):
    """``facets.json``: every decided entity, by slug."""

    consolidator_versions: dict[str, str] = Field(
        default_factory=dict, description="Kind group → version, as of the last run."
    )
    model: str = Field("", description="Model of the last run.")
    entities: dict[str, WcsWikiFacetDecision] = Field(
        default_factory=dict, description="Entity slug → its decision."
    )


# ── Generation: prose.json ──


class WcsWikiProseRevision(BaseModel):
    """One version of an entity's prose: generated, or found edited on the page."""

    by: Literal["llm", "page"] = Field(..., description="Who wrote it.")
    at: dt.date = Field(..., description="When.")
    model: str | None = Field(None, description="Model, for generated revisions.")
    statements: int | None = Field(
        None, description="Statements integrated, for generated revisions."
    )
    definition: str | None = Field(None, description="The definition.")
    context: str | None = Field(None, description="The In practice paragraph.")
    prose: str | None = Field(
        None, description="Single-field prose of the earliest revisions."
    )


class WcsWikiProseEntry(BaseModel):
    """One entity's current prose, its watermark and its history."""

    entity_id: uuid.UUID = Field(..., description="The entity (or proposed concept).")
    generator_version: str = Field(..., description="Generator version.")
    model: str = Field("", description="Model of the current prose.")
    generated_at: dt.date | None = Field(None, description="When it was generated.")
    definition: str = Field("", description="Current definition.")
    context: str = Field("", description="Current In practice paragraph.")
    integrated: list[str] = Field(
        default_factory=list, description="Statement keys integrated, sorted."
    )
    revisions: list[WcsWikiProseRevision] = Field(
        default_factory=list, description="Every revision, oldest first."
    )


class WcsWikiProseState(BaseModel):
    """``prose.json``: every generated entity, by slug."""

    generator_version: str = Field(
        ..., description="Generator version of the last run."
    )
    model: str = Field("", description="Model of the last run.")
    entities: dict[str, WcsWikiProseEntry] = Field(
        default_factory=dict, description="Entity slug → its prose."
    )


class WcsWikiStateWriteResult(BaseModel):
    """What a state ``POST`` stored."""

    written: int = Field(..., description="Entries upserted.")
    total: int = Field(..., description="Entries in the store afterwards.")


# ── Wiki jobs: the wiki-curator queue (ADR-006, MINI-95) ──


class WcsWikiJob(BaseModel):
    """One unit of wiki work, as the curator's worker runs it.

    ``link`` names a source; ``consolidate`` and ``generate`` name entities
    (``generate`` also accepts one ``entity``). The worker refuses a job
    missing what its kind needs.
    """

    job: Literal["link", "consolidate", "generate"] = Field(
        ..., description="Which pass runs."
    )
    source_id: uuid.UUID | None = Field(None, description="link: the source.")
    entity: str | None = Field(None, description="generate: one entity slug.")
    entities: list[str] | None = Field(
        None, description="consolidate / generate: entity slugs."
    )


class WcsWikiJobsRequest(BaseModel):
    """``POST /v1/wcs/wiki/jobs`` — jobs to put on the wiki-curator queue.

    The curator's worker sends the follow-on jobs a job named; an operator
    can send any job by hand.
    """

    jobs: list[WcsWikiJob] = Field(
        ..., min_length=1, max_length=100, description="Jobs, in order."
    )


class WcsWikiJobsAccepted(BaseModel):
    """What was enqueued: one message id per job, in order."""

    message_ids: list[str] = Field(..., description="SQS message ids.")
