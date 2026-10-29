"""The catalog lookup contract: tracks in, the entry each belongs to out."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    CATALOG_LOOKUP_MAX_TRACKS,
    ENDPOINTS_BY_NAME,
    CatalogLookupRequest,
    CatalogMatch,
    DeejayRunRequest,
    IngestTrack,
)

_META = {"count": 2, "total": 2, "version": "test"}
_ENTRY = "3f2b9c1e-7a4d-4e8b-9c1f-2a3b4c5d6e7f"


def _track(**overrides: Any) -> IngestTrack:
    base: dict[str, Any] = {"title": "Temple", "artist": "Thomas Gold"}
    base.update(overrides)
    return IngestTrack(**base)


def test_the_endpoint_is_a_post_under_the_catalog_write_scope() -> None:
    endpoint = ENDPOINTS_BY_NAME["lookup_catalog"]
    assert endpoint.method == "POST"
    assert endpoint.path == "/v1/catalog/lookup"
    assert endpoint.request is CatalogLookupRequest
    assert endpoint.scope == "catalog.sets.write"


def test_a_lookup_takes_between_one_and_the_most_tracks() -> None:
    with pytest.raises(ValidationError):
        CatalogLookupRequest(tracks=[])
    with pytest.raises(ValidationError):
        CatalogLookupRequest(tracks=[_track()] * (CATALOG_LOOKUP_MAX_TRACKS + 1))
    assert len(CatalogLookupRequest(tracks=[_track()]).tracks) == 1


def test_a_lookup_refuses_fields_it_does_not_know() -> None:
    with pytest.raises(ValidationError):
        CatalogLookupRequest.model_validate({"tracks": [], "owner": "x"})


def test_the_client_sends_the_tracks_and_reads_one_answer_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    answer = {
        "data": [
            {
                "id": _ENTRY,
                "title": "Temple",
                "remix": "Niko The Kid Remix",
                "artist": "Thomas Gold",
                "label": "Spinnin'",
                "genre": "Dance",
                "bpm": 98.0,
                "release_year": 2021,
                "play_count": 12,
            },
            None,
        ],
        "meta": _META,
    }

    def fake_post(self, path, payload):  # noqa: ARG001
        calls.append((path, payload))
        return answer

    monkeypatch.setattr(KaianoApiClient, "post", fake_post)
    client = KaianoApiClient(base_url="https://x", api_key="k")

    found = client.lookup_catalog(
        CatalogLookupRequest(
            tracks=[_track(remix="Niko The Kid Remix"), _track(title="Unknown")]
        )
    )

    path, payload = calls[0]
    assert path == "/v1/catalog/lookup"
    assert payload == {
        "tracks": [
            {"title": "Temple", "artist": "Thomas Gold", "remix": "Niko The Kid Remix"},
            {"title": "Unknown", "artist": "Thomas Gold"},
        ]
    }
    assert isinstance(found[0], CatalogMatch)
    assert str(found[0].id) == _ENTRY
    assert found[0].remix == "Niko The Kid Remix"
    assert found[1] is None


def test_deejay_cog_can_be_asked_to_retag_music() -> None:
    assert DeejayRunRequest(mode="retag-music").mode == "retag-music"
    with pytest.raises(ValidationError):
        DeejayRunRequest(mode="retag-everything")  # type: ignore[arg-type]
