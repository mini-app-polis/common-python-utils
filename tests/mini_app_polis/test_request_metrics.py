"""Request metrics: what is recorded, and what reaches CloudWatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from mini_app_polis.request_metrics import NAMESPACE, RequestMetricsMiddleware


class FakeCloudWatch:
    """Just enough of the CloudWatch client: records put_metric_data calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def put_metric_data(self, *, Namespace: str, MetricData: list[dict]) -> None:
        if self.fail:
            raise RuntimeError("throttled")
        self.calls.append({"Namespace": Namespace, "MetricData": MetricData})


@dataclass
class FakeRoute:
    path: str


def make_app(
    status: int = 200,
    *,
    route: str | None = None,
    effective: str | None = None,
    raises: bool = False,
):
    """A bare ASGI app that sets the scope's route the way FastAPI's router does.

    ``effective`` is the prefixed template FastAPI 0.13x+ records for a route
    included under a prefix, while ``route`` stays unprefixed.
    """

    async def app(scope, receive, send):
        if route is not None:
            scope["route"] = FakeRoute(route)
        if effective is not None:
            scope["fastapi"] = {"effective_route_context": FakeRoute(effective)}
        if raises:
            raise RuntimeError("boom")
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def call(middleware: RequestMetricsMiddleware, path: str = "/v1/things") -> None:
    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(_message):
        return None

    asyncio.run(
        middleware({"type": "http", "method": "GET", "path": path}, receive, send)
    )


def build(app=None, client: FakeCloudWatch | None = None, **kwargs):
    client = client or FakeCloudWatch()
    middleware = RequestMetricsMiddleware(
        app or make_app(),
        service="api-test",
        client_factory=lambda: client,
        start_flusher=False,
        **kwargs,
    )
    return middleware, client


def datums(client: FakeCloudWatch) -> list[dict[str, Any]]:
    return [d for c in client.calls for d in c["MetricData"]]


def by_name(client: FakeCloudWatch, name: str) -> list[dict[str, Any]]:
    return [d for d in datums(client) if d["MetricName"] == name]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "true")


def test_gated_off_passes_through_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDWATCH_METRICS_ENABLED", "false")
    middleware, client = build()
    call(middleware)
    assert middleware.flush() == 0
    assert client.calls == []


def test_publishes_latency_and_error_counts() -> None:
    middleware, client = build()
    call(middleware)
    call(middleware)
    middleware.record(None, 503, 12.4)
    middleware.record(None, 404, 3.0)

    assert middleware.flush() == 3
    assert client.calls[0]["Namespace"] == NAMESPACE

    (latency,) = by_name(client, "Latency")
    assert latency["Dimensions"] == [{"Name": "Service", "Value": "api-test"}]
    assert latency["Unit"] == "Milliseconds"
    assert sum(latency["Counts"]) == 4
    assert by_name(client, "Errors5xx")[0]["Value"] == 1
    assert by_name(client, "Errors4xx")[0]["Value"] == 1


def test_error_counts_are_zero_not_absent_when_there_were_requests() -> None:
    middleware, client = build()
    call(middleware)
    middleware.flush()
    assert by_name(client, "Errors5xx")[0]["Value"] == 0
    assert by_name(client, "Errors4xx")[0]["Value"] == 0


def test_nothing_published_for_an_empty_minute() -> None:
    middleware, client = build()
    assert middleware.flush() == 0
    assert client.calls == []


def test_listed_route_gets_its_own_series() -> None:
    middleware, client = build(
        make_app(route="/v1/sets/{set_id}"), routes=["/v1/sets/{set_id}"]
    )
    call(middleware, "/v1/sets/123")
    middleware.flush()
    dims = [d["Dimensions"] for d in by_name(client, "Latency")]
    assert [{"Name": "Service", "Value": "api-test"}] in dims
    assert [
        {"Name": "Service", "Value": "api-test"},
        {"Name": "Route", "Value": "/v1/sets/{set_id}"},
    ] in dims


def test_prefixed_route_matches_by_its_full_template() -> None:
    # FastAPI 0.141: an included router's route reports "/evaluations"; the
    # "/v1" prefix is only on the effective route context.
    middleware, client = build(
        make_app(route="/evaluations", effective="/v1/evaluations"),
        routes=["/v1/evaluations"],
    )
    call(middleware, "/v1/evaluations")
    middleware.flush()
    dims = [d["Dimensions"] for d in by_name(client, "Latency")]
    assert [
        {"Name": "Service", "Value": "api-test"},
        {"Name": "Route", "Value": "/v1/evaluations"},
    ] in dims


def test_unlisted_route_counts_only_toward_the_service() -> None:
    middleware, client = build(make_app(route="/v1/other"), routes=["/v1/sets"])
    call(middleware)
    middleware.flush()
    assert len(by_name(client, "Latency")) == 1


def test_excluded_path_is_not_recorded() -> None:
    middleware, client = build(exclude_paths=["/health"])
    call(middleware, "/health")
    assert middleware.flush() == 0


def test_exception_before_response_counts_as_5xx_and_is_reraised() -> None:
    middleware, client = build(make_app(raises=True))
    with pytest.raises(RuntimeError, match="boom"):
        call(middleware)
    middleware.flush()
    assert by_name(client, "Errors5xx")[0]["Value"] == 1


def test_failed_publish_is_dropped_not_raised() -> None:
    middleware, client = build(client=FakeCloudWatch(fail=True))
    call(middleware)
    assert middleware.flush() == 0
    # Dropped, not carried into the next window.
    assert middleware.flush() == 0


def test_client_factory_failure_is_dropped_not_raised() -> None:
    def broken():
        raise RuntimeError("no credentials")

    middleware = RequestMetricsMiddleware(
        make_app(), service="api-test", client_factory=broken, start_flusher=False
    )
    call(middleware)
    assert middleware.flush() == 0


def test_more_than_150_distinct_latencies_split_across_datums() -> None:
    middleware, client = build()
    for ms in range(400):
        middleware.record(None, 200, ms)
    middleware.flush()
    latency = by_name(client, "Latency")
    assert all(len(d["Values"]) <= 150 for d in latency)
    assert sum(sum(d["Counts"]) for d in latency) == 400


def test_non_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware, client = build(app)

    async def noop(*_):
        return None

    asyncio.run(middleware({"type": "lifespan"}, noop, noop))
    assert seen == ["lifespan"]
    assert middleware.flush() == 0


def test_slow_request_is_logged_with_its_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware, _ = build(
        make_app(route="/sets/{set_id}", effective="/v1/sets/{set_id}"),
        slow_request_ms=0,
    )
    with caplog.at_level("WARNING", logger="mini_app_polis.request_metrics"):
        call(middleware, "/v1/sets/123")
    (message,) = [r.getMessage() for r in caplog.records]
    assert "slow request GET /v1/sets/{set_id} -> 200" in message


def test_fast_request_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    middleware, _ = build()
    with caplog.at_level("WARNING", logger="mini_app_polis.request_metrics"):
        call(middleware)
    assert caplog.records == []


def test_slow_logging_can_be_turned_off(caplog: pytest.LogCaptureFixture) -> None:
    middleware, _ = build(slow_request_ms=None)
    with caplog.at_level("WARNING", logger="mini_app_polis.request_metrics"):
        call(middleware)
    assert caplog.records == []
