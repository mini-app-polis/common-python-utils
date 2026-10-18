"""Contract for the wiki curator's state, its page documents and its jobs.

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


# ── Page documents: what materialise writes (ADR-006, MINI-96) ──
#
# One document per entity page, built by the curator, which owns the page
# logic (folds, the reference overlay, By teacher). The API stores, filters
# and serves the documents; it never recomputes them. Text fields are
# markdown. Links in them are neutral: ``[[slug]]`` or ``[[slug|label]]`` for
# a page, ``[[source:<uuid>]]`` or ``[[source:<uuid>|label]]`` for a source,
# which each reader rewrites to its own URLs.


class WcsWikiPageCitation(BaseModel):
    """The source a piece of evidence came from.

    ``public`` is the source's default visibility when the page was
    materialised. Readers that filter go by the source's current visibility,
    not this snapshot: a source can be made private after its pages were
    written.
    """

    source_id: uuid.UUID = Field(..., description="The source.")
    source_slug: str = Field(..., description="The source's page slug in the export.")
    session_date: dt.date | None = Field(None, description="The source's date.")
    public: bool = Field(..., description="Default-visible when materialised.")


class WcsWikiPageStatement(BaseModel):
    """One thing a teacher said about the page's entity."""

    kind: Literal["attribution", "definition"] = Field(
        ..., description="An attribution (taught, drilled…) or a definition."
    )
    term: str = Field("", description="The entity as named in the lesson.")
    text: str = Field("", description="What was said.")
    goal: str = Field("", description="A drill's goal, when it has one.")
    steps: list[str] = Field(default_factory=list, description="A drill's steps.")
    citation: WcsWikiPageCitation = Field(..., description="Where it was said.")


class WcsWikiPageTeacher(BaseModel):
    """Everything one teacher said about the entity, in source order."""

    name: str = Field(..., description="The teacher, as the page heads them.")
    statements: list[WcsWikiPageStatement] = Field(
        default_factory=list, description="Their statements."
    )


class WcsWikiPageMistake(BaseModel):
    """A common mistake, with its correction when there is one."""

    text: str = Field(..., description="Mistake → correction, as one line.")
    teacher: str = Field("", description="Who pointed it out.")
    citation: WcsWikiPageCitation = Field(..., description="Where.")


class WcsWikiPageSkill(BaseModel):
    """A skill a drill develops, or one a technique requires."""

    skill: str = Field(..., description="The skill, as extracted (a sentence).")
    text: str = Field("", description="What was said about it.")
    focus: str = Field("", description="When to focus on it (drills only).")
    citation: WcsWikiPageCitation | None = Field(
        None, description="Where, when the row names its source."
    )


class WcsWikiPageLink(BaseModel):
    """Another page, as this page refers to it."""

    slug: str = Field(..., description="The page's slug.")
    kind: str = Field(..., description="The page's kind.")
    name: str = Field(..., description="The page's display name.")
    count: int = Field(1, description="Statements behind the link.")


class WcsWikiPageRelated(BaseModel):
    """A relation between this entity and another, from a lesson or a note."""

    slug: str = Field(..., description="The other entity's slug.")
    kind: str = Field(..., description="The other entity's kind.")
    name: str = Field(..., description="The other entity's display name.")
    relation: str = Field(..., description="The relation kind, e.g. variation-of.")
    text: str = Field("", description="The relation described.")
    citation: WcsWikiPageCitation | None = Field(
        None, description="Where, when the relation came from a source."
    )


class WcsWikiPageDetail(BaseModel):
    """A narrower entity folded into this page (a facet), in one line."""

    slug: str = Field(..., description="The facet's own slug.")
    name: str = Field(..., description="The facet's name.")
    gist: str = Field("", description="First sentence of its definition.")
    depth: int = Field(0, description="Nesting under another facet; 0 is top.")


class WcsWikiPage(BaseModel):
    """One entity page, as structured data rather than rendered markup."""

    slug: str = Field(..., description="The page's slug; its identity.")
    name: str = Field(..., description="Display name.")
    kind: str = Field(..., description="concept, technique, pattern or drill.")
    status: str = Field("", description="The entity's status.")
    aliases: list[str] = Field(default_factory=list, description="Other names.")
    teachers: list[str] = Field(
        default_factory=list, description="Slugs of instructors with evidence here."
    )
    definition: str = Field("", description="The page's first section.")
    in_practice: str = Field("", description="What a dancer needs beyond it.")
    details: list[WcsWikiPageDetail] = Field(
        default_factory=list, description="Facets folded in, in reading order."
    )
    by_teacher: list[WcsWikiPageTeacher] = Field(
        default_factory=list, description="Statements grouped by teacher, by name."
    )
    mistakes: list[WcsWikiPageMistake] = Field(
        default_factory=list, description="Common mistakes."
    )
    develops: list[WcsWikiPageSkill] = Field(
        default_factory=list, description="Drills: the skills it trains."
    )
    requires: list[WcsWikiPageSkill] = Field(
        default_factory=list, description="Techniques: the skills it needs."
    )
    related: list[WcsWikiPageRelated] = Field(
        default_factory=list, description="Relations touching the entity."
    )
    mentions: list[WcsWikiPageLink] = Field(
        default_factory=list, description="Pages its statements refer to."
    )
    referenced_by: list[WcsWikiPageLink] = Field(
        default_factory=list,
        description="Every page referring to it, most-referenced first. Readers "
        "may show only the count past a few dozen.",
    )
    sources: list[WcsWikiPageCitation] = Field(
        default_factory=list,
        description="Sources of its own attributions and definitions, by slug.",
    )
    evidence_sources: list[uuid.UUID] = Field(
        default_factory=list,
        description="Every source its prose was built from: its own statements "
        "and the statements elsewhere that refer to it. A reader who can see "
        "none of them does not see the page.",
    )
    folded: list[str] = Field(
        default_factory=list,
        description="Every slug folded into this page: its facets (also in "
        "details) and other names for the same idea. They have no page of "
        "their own; a lookup of one is served this page (MINI-100).",
    )
    requested_slug: str | None = Field(
        default=None,
        description="Set by the API on a read, never stored: the slug asked "
        "for, when it is one folded into this page. None when the page was "
        "asked for by its own slug.",
    )


class WcsWikiPagesWrite(BaseModel):
    """``POST /v1/wcs/wiki/pages`` — upsert pages, and remove pages gone.

    Each page given replaces the stored one with its slug. A slug in
    ``remove`` is a page that no longer exists — folded into another, or no
    longer in the corpus — and is deleted. Re-sending is a no-op.
    """

    pages: list[WcsWikiPage] = Field(
        default_factory=list, max_length=200, description="Pages to store."
    )
    remove: list[str] = Field(
        default_factory=list, max_length=500, description="Slugs to delete."
    )


class WcsWikiPagesWriteResult(BaseModel):
    """What a pages ``POST`` stored."""

    written: int = Field(..., description="Pages upserted.")
    removed: int = Field(..., description="Pages deleted.")
    total: int = Field(..., description="Pages in the store afterwards.")


# ── Wiki jobs: the wiki-curator queue (ADR-006, MINI-95) ──


class WcsWikiJob(BaseModel):
    """One unit of wiki work, as the curator's worker runs it.

    ``link`` names a source; ``consolidate``, ``generate`` and
    ``materialise`` name entities (``generate`` also accepts one
    ``entity``). The worker refuses a job missing what its kind needs.
    """

    job: Literal["link", "consolidate", "generate", "materialise"] = Field(
        ..., description="Which pass runs."
    )
    source_id: uuid.UUID | None = Field(None, description="link: the source.")
    entity: str | None = Field(None, description="generate: one entity slug.")
    entities: list[str] | None = Field(
        None, description="consolidate / generate / materialise: entity slugs."
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
