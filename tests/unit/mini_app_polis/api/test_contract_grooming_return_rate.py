"""The return-from-Ready read: moves to Ready, returns, and their ratio."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    ENDPOINTS_BY_NAME,
    RETURN_RATE_MAX_DAYS,
    GroomingReturnRate,
)

_META = {"count": 1, "total": 1, "version": "test"}
_ANSWER = {
    "days": 30,
    "since": "2026-09-29T12:00:00Z",
    "counting_since": "2026-10-20T08:00:00Z",
    "moved_to_ready": 8,
    "returned": 2,
    "ratio": 0.25,
}


def test_the_endpoint_is_a_read_under_the_groomings_scope() -> None:
    endpoint = ENDPOINTS_BY_NAME["grooming_return_rate"]
    assert endpoint.method == "GET"
    assert endpoint.path == "/v1/tasks/groomings/return-rate"
    assert endpoint.response is GroomingReturnRate
    assert endpoint.scope == "tasks.groomings.read"


def test_nothing_moved_means_no_ratio() -> None:
    rate = GroomingReturnRate.model_validate(
        {**_ANSWER, "moved_to_ready": 0, "returned": 0, "ratio": None}
    )
    assert rate.ratio is None


@pytest.mark.parametrize("days", [0, RETURN_RATE_MAX_DAYS + 1])
def test_the_window_is_bounded(days: int) -> None:
    with pytest.raises(ValidationError):
        GroomingReturnRate.model_validate({**_ANSWER, "days": days})


@pytest.mark.parametrize(("days", "params"), [(None, None), (7, {"days": 7})])
def test_the_client_asks_for_the_window(
    monkeypatch: pytest.MonkeyPatch, days: int | None, params: Any
) -> None:
    calls: list[tuple[str, Any]] = []

    def fake_get(self, path, params=None):  # noqa: ARG001
        calls.append((path, params))
        return {"data": _ANSWER, "meta": _META}

    monkeypatch.setattr(KaianoApiClient, "get", fake_get)
    client = KaianoApiClient(base_url="https://x", api_key="k")

    rate = client.grooming_return_rate(days)

    assert calls == [("/v1/tasks/groomings/return-rate", params)]
    assert rate.moved_to_ready == 8
    assert rate.returned == 2
    assert rate.ratio == 0.25
