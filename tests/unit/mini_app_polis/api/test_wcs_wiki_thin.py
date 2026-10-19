"""Thin entities and page provenance travel whole (MINI-101)."""

from __future__ import annotations

import uuid

from mini_app_polis.api.contract import (
    ENDPOINTS,
    WcsWikiJob,
    WcsWikiPagesWrite,
    WcsWikiThinState,
    WcsWikiThinWrite,
)

SOURCE = "b436c89d-72ba-4d4d-8bef-f6957bd3643f"


def test_a_thin_list_round_trips() -> None:
    raw = {
        "entities": [
            {
                "slug": "stationary-anchor",
                "name": "stationary anchor",
                "kind": "concept",
                "sources": 1,
                "references": 0,
                "page": "anchor",
                "same": False,
                "definition": "An anchor that stays put.",
                "quotes": [
                    {
                        "text": "Keep the anchor where it lands.",
                        "source_id": SOURCE,
                        "session_date": "2026-09-16",
                        "source_title": "Anchors",
                    }
                ],
            }
        ]
    }
    assert WcsWikiThinState.model_validate(raw).model_dump(mode="json") == raw


def test_a_thin_write_defaults_to_nothing() -> None:
    write = WcsWikiThinWrite()
    assert (write.entities, write.remove) == ([], [])


def test_a_job_and_a_pages_write_carry_their_origin() -> None:
    origin = uuid.UUID(SOURCE)
    job = WcsWikiJob(job="materialise", entities=["anchor"], origin_source_id=origin)
    assert WcsWikiJob.model_validate(job.model_dump(mode="json")) == job

    write = WcsWikiPagesWrite(origin_source_id=origin, origin_job_id="msg-1")
    assert write.model_dump(mode="json")["origin_source_id"] == SOURCE
    # Work an admin started has no origin.
    assert WcsWikiPagesWrite().origin_source_id is None
    assert WcsWikiJob(job="materialise").origin_source_id is None


def test_the_thin_endpoints_are_in_the_catalog() -> None:
    by_name = {e.name: e for e in ENDPOINTS}
    assert by_name["get_wcs_wiki_thin"].scope == "wcs.corpus.read"
    assert by_name["put_wcs_wiki_thin"].scope == "wcs.wiki.write"
    assert by_name["put_wcs_wiki_thin"].path == "/v1/wcs/wiki/state/thin"
