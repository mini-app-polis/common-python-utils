"""Contract for pipeline evaluation findings.

``POST /v1/evaluations`` (evaluator-cog, wiki-curator-cog and
:func:`mini_app_polis.pipeline_status.post_findings`) and ``GET /v1/evaluations``
(evaluator-cog).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PipelineEvaluationCreate(BaseModel):
    """Payload for creating one pipeline evaluation finding."""

    run_id: str | None = Field(default=None, description="Semantic value for run id.")
    violation_id: str | None = Field(
        default=None, description="Semantic value for violation id."
    )
    repo: str = Field(..., description="Semantic value for repo.")
    dimension: str = Field(
        ..., description="Semantic value for dimension."
    )  # structural_conformance | pipeline_consistency |
    # testing_coverage | documentation_coverage |
    # cd_readiness | cross_repo_coherence | standards_currency
    severity: Literal["CRITICAL", "ERROR", "WARN", "INFO", "SUCCESS"] = Field(
        ..., description="Semantic value for severity."
    )
    finding: str = Field(..., description="Semantic value for finding.")
    suggestion: str | None = Field(
        default=None, description="Semantic value for suggestion."
    )
    standards_version: str | None = Field(
        default=None,
        description=(
            "Standards-version this finding was evaluated against. Only "
            "meaningful for conformance-evaluator paths (LLM and "
            "deterministic) which know the standards rev they ran against. "
            "Self-reported runs from pipeline cogs "
            "(source=flow_inline / flow_hook) don't run against any "
            "standards rev and leave this null. The previous default of "
            "'6.0' stamped a stale version onto every self-report, which "
            "then surfaced in Pipeline Health as 'Evaluated against: v6.0' "
            "for runs that hadn't been evaluated against any standards "
            "at all."
        ),
    )
    evaluator_version: str | None = Field(
        default=None,
        description=(
            "Release of evaluator-cog that produced this finding. Distinct "
            "from standards_version: the catalog and the evaluator release "
            "separately, and a finding's wording comes from the evaluator. "
            "Null for self-reported runs from pipeline cogs."
        ),
    )
    source: str | None = Field(default=None, description="Semantic value for source.")
    flow_name: str | None = Field(
        default=None, description="Semantic value for flow name."
    )

    model_config = ConfigDict(extra="forbid")


class PipelineEvaluationItem(BaseModel):
    """Pipeline evaluation record returned by API routes."""

    id: uuid.UUID = Field(
        ..., description="Unique identifier for this pipelineevaluation."
    )
    run_id: str | None = Field(..., description="Semantic value for run id.")
    violation_id: str | None = Field(
        default=None, description="Semantic value for violation id."
    )
    repo: str = Field(..., description="Semantic value for repo.")
    dimension: str = Field(..., description="Semantic value for dimension.")
    severity: str = Field(..., description="Semantic value for severity.")
    finding: str = Field(..., description="Semantic value for finding.")
    suggestion: str | None = Field(..., description="Semantic value for suggestion.")
    standards_version: str | None = Field(
        ..., description="Semantic value for standards version."
    )
    evaluator_version: str | None = Field(
        default=None, description="Release of evaluator-cog that wrote the finding."
    )
    source: str | None = Field(default=None, description="Semantic value for source.")
    flow_name: str | None = Field(
        default=None, description="Semantic value for flow name."
    )
    evaluated_at: dt.datetime = Field(
        ..., description="Semantic value for evaluated at."
    )


class PipelineEvaluationWriteResult(PipelineEvaluationItem):
    """What one write did, which is not the same as what is stored.

    A write that matched a finding already held under this run returns the
    stored row with ``deduplicated`` set, rather than a second copy of it.
    """

    deduplicated: bool = Field(
        default=False,
        description=(
            "True when this finding was already stored under this run and "
            "nothing new was written; the row returned is the one that was "
            "already there. Callers that count a write as a delivered "
            "finding must read this — a 200 alone no longer means a row "
            "was created."
        ),
    )
