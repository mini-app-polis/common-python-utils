"""Page documents and the materialise job (wiki-curator-cog ADR-006)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mini_app_polis.api.contract import (
    WcsWikiJob,
    WcsWikiPage,
    WcsWikiPagesWrite,
)

SOURCE = "b436c89d-72ba-4d4d-8bef-f6957bd3643f"
CITATION = {
    "source_id": SOURCE,
    "source_slug": "2026-09-16-connection",
    "session_date": "2026-09-16",
    "public": False,
}


def _page() -> dict:
    return {
        "slug": "anchor",
        "name": "Anchor",
        "kind": "concept",
        "status": "stub",
        "aliases": ["anchor step"],
        "teachers": ["kaiano-levine"],
        "definition": "The end of a pattern; see [[connection|connection]].",
        "in_practice": "Settle as taught in [[source:" + SOURCE + "|this lesson]].",
        "details": [
            {
                "slug": "stationary-anchor",
                "name": "stationary anchor",
                "gist": "",
                "depth": 0,
            }
        ],
        "by_teacher": [
            {
                "name": "Kaiano Levine",
                "statements": [
                    {
                        "kind": "attribution",
                        "term": "anchor",
                        "text": "Finish the pattern.",
                        "goal": "",
                        "steps": [],
                        "citation": CITATION,
                    }
                ],
            }
        ],
        "mistakes": [
            {"text": "Rushing → wait", "teacher": "Kaiano", "citation": CITATION}
        ],
        "develops": [],
        "requires": [],
        "related": [
            {
                "slug": "frame",
                "kind": "concept",
                "name": "Frame",
                "relation": "contrasts-with",
                "text": "",
                "citation": None,
            }
        ],
        "mentions": [
            {"slug": "connection", "kind": "concept", "name": "Connection", "count": 2}
        ],
        "referenced_by": [],
        "sources": [CITATION],
        "evidence_sources": [SOURCE],
        "folded": ["anchor-step-variation", "stationary-anchor"],
        "requested_slug": None,
    }


def test_a_page_round_trips() -> None:
    raw = _page()
    assert WcsWikiPage.model_validate(raw).model_dump(mode="json") == raw


def test_a_page_stored_before_folded_existed_still_reads() -> None:
    # Documents written before folds were recorded have neither field.
    raw = _page()
    del raw["folded"], raw["requested_slug"]
    page = WcsWikiPage.model_validate(raw)
    assert page.folded == []
    assert page.requested_slug is None


def test_a_write_carries_pages_and_removals() -> None:
    write = WcsWikiPagesWrite.model_validate({"pages": [_page()], "remove": ["old"]})
    assert [p.slug for p in write.pages] == ["anchor"]
    assert write.remove == ["old"]


def test_a_citation_needs_its_visibility() -> None:
    raw = _page()
    del raw["sources"][0]["public"]
    with pytest.raises(ValidationError):
        WcsWikiPage.model_validate(raw)


def test_materialise_is_a_job() -> None:
    job = WcsWikiJob.model_validate({"job": "materialise", "entities": ["anchor"]})
    assert job.job == "materialise"
