"""The wiki curator's state documents keep every field the files hold."""

from __future__ import annotations

from typing import Any

from mini_app_polis.api.contract import (
    WcsWikiFacetsState,
    WcsWikiLinksState,
    WcsWikiProseState,
)

KEY = "attribution:0012e47a-7fce-4c01-9322-0a446fb8e05f"


def _same(model: Any, raw: dict[str, Any]) -> None:
    """The document dumps back to the file's own JSON, nulls aside."""
    assert model.model_validate(raw).model_dump(mode="json", exclude_none=True) == (
        _drop_none(raw)
    )


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def test_a_links_file_round_trips() -> None:
    _same(
        WcsWikiLinksState,
        {
            "linker_version": "3",
            "model": "anthropic/claude-sonnet-4-6",
            "statements": {
                KEY: {
                    "linker_version": "3",
                    "model": "anthropic/claude-sonnet-4-6",
                    "subject_slug": "closed-position-frame",
                    "source_id": "b436c89d-72ba-4d4d-8bef-f6957bd3643f",
                    "session_date": "2026-09-16",
                    "text": "closed position frame — connect at the shoulder blade.",
                    "references": [
                        {
                            "name": "connection",
                            "target_slug": "connection",
                            "target_entity_id": None,
                            "polarity": "assert",
                        },
                        {
                            "name": "frame",
                            "target_slug": "frame",
                            "target_entity_id": "30f62150-c875-445e-a0c7-33c3c924548b",
                            "polarity": "deny",
                        },
                    ],
                }
            },
        },
    )


def test_a_facets_file_round_trips() -> None:
    _same(
        WcsWikiFacetsState,
        {
            "consolidator_versions": {"concept": "5", "drill": "5", "pattern": "5"},
            "model": "m",
            "entities": {
                "stationary-anchor": {
                    "parent": "anchor",
                    "same": False,
                    "reason": "a kind of anchor",
                    "version": "5",
                    "model": "m",
                    "decided_at": "2026-10-16",
                },
                "anchor": {
                    "parent": None,
                    "same": False,
                    "reason": "",
                    "version": "5",
                    "model": "m",
                    "decided_at": "2026-10-16",
                },
            },
        },
    )


def test_a_prose_file_round_trips_every_revision_shape() -> None:
    _same(
        WcsWikiProseState,
        {
            "generator_version": "4",
            "model": "m",
            "entities": {
                "anchor": {
                    "entity_id": "30f62150-c875-445e-a0c7-33c3c924548b",
                    "generator_version": "4",
                    "model": "m",
                    "generated_at": "2026-10-16",
                    "definition": "The settle.",
                    "context": "",
                    "integrated": [KEY],
                    "revisions": [
                        # the earliest, single-field shape
                        {
                            "by": "llm",
                            "at": "2026-10-16",
                            "model": "m",
                            "statements": 2,
                            "prose": "Old.",
                        },
                        {
                            "by": "page",
                            "at": "2026-10-16",
                            "definition": "Edited.",
                            "context": "",
                        },
                        {
                            "by": "llm",
                            "at": "2026-10-16",
                            "model": "m",
                            "statements": 1,
                            "definition": "The settle.",
                            "context": "",
                        },
                    ],
                }
            },
        },
    )
