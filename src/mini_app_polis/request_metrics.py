"""Request latency and error counts for an ASGI API, published to CloudWatch.

Why this exists. Railway shows an API's CPU and memory but nothing about
its requests: no latency percentiles, no error rate. This middleware
records every request's duration and status in memory and a background
thread publishes them once a minute, so CloudWatch can compute p50, p95
and p99 over any window and the API sits on the same dashboard as the
Lambda cogs.

What it publishes, under the ``MiniAppPolis/Api`` namespace with a
``Service`` dimension:

``Latency`` (milliseconds)
    Every request's duration, sent as raw values rather than a pre-computed
    average, so any percentile can be taken afterwards. Its SampleCount is
    the request count, so request rate needs no metric of its own.
``Errors5xx`` / ``Errors4xx`` (count)
    Published, zeros included, for every minute that had requests, so an
    error *rate* is ``Errors5xx / Latency SampleCount`` in metric math.

``routes`` names route templates (``/v1/evaluations/runs``) that also get
their own ``Latency`` series with a ``Route`` dimension. It is an allow
list on purpose: CloudWatch bills each dimension value as a separate
metric, so the metric count stays fixed however many paths are hit. The
template comes from the matched route, never the raw path, so
``/v1/sets/123`` and ``/v1/sets/456`` are one series.

Nothing here sits in a request's hot path beyond a dictionary update. A
failed publish logs and drops that minute; it never raises into a request
and never blocks one. Recording is gated by ``Effect.CLOUDWATCH_METRICS``,
so outside production the middleware passes requests straight through.

The CloudWatch client is the caller's: ``client_factory`` returns one when
the first publish happens. boto3 is not a dependency of this package, and
the caller is the one that knows which credentials to use.

Usage::

    app.add_middleware(
        RequestMetricsMiddleware,
        service="api-kaianolevine-com",
        client_factory=lambda: boto3.client("cloudwatch", ...),
        exclude_paths=["/health"],
    )
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from .environment import Effect, effect_enabled

NAMESPACE = "MiniAppPolis/Api"

#: One publish a minute: CloudWatch's standard resolution, and one
#: PutMetricData call per API per minute is well inside the free tier.
DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0

#: PutMetricData limits: distinct values per datum, datums per call.
_MAX_VALUES_PER_DATUM = 150
_MAX_DATUMS_PER_CALL = 1000

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_log = logging.getLogger(__name__)


class _Window:
    """One minute's requests, as counts. Swapped out whole at each publish."""

    def __init__(self) -> None:
        #: Route template, or None for the service-wide series → ms → count.
        self.latency: dict[str | None, Counter[int]] = {}
        self.errors_4xx = 0
        self.errors_5xx = 0

    @property
    def empty(self) -> bool:
        """Whether no request was recorded in this window.

        Every request adds to the service-wide latency series, errors
        included, so no latency means nothing to publish: ``flush`` skips
        the PutMetricData call rather than sending an empty minute.
        """
        return not self.latency


def _route_template(scope: Scope) -> str | None:
    """The matched route's full template, prefixes included, or None.

    FastAPI 0.13x and later keep a route included under a prefix unprefixed
    — ``scope["route"].path`` is ``/evaluations`` for ``/v1/evaluations`` —
    and record the prefixed template on the effective route context. Read
    that first; ``scope["route"]`` is the answer on older FastAPI and plain
    Starlette. Reading the route alone made every configured ``/v1/...``
    route silently fail to match, so its series never appeared.
    """
    fastapi_scope = scope.get("fastapi")
    context = (
        fastapi_scope.get("effective_route_context")
        if isinstance(fastapi_scope, dict)
        else None
    )
    for candidate in (context, scope.get("route")):
        path = getattr(candidate, "path_format", None) or getattr(
            candidate, "path", None
        )
        if isinstance(path, str) and path:
            return path
    return None


class RequestMetricsMiddleware:
    """Pure ASGI middleware: records each HTTP request, publishes each minute.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because the route template
    is only in the scope after routing has run, and a wrapped scope is the
    one place both the template and the status on the wire are visible.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        client_factory: Callable[[], Any],
        routes: Iterable[str] = (),
        exclude_paths: Iterable[str] = (),
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        start_flusher: bool = True,
    ) -> None:
        self.app = app
        self.service = service
        self.routes = frozenset(routes)
        self.exclude_paths = frozenset(exclude_paths)
        self.enabled = effect_enabled(Effect.CLOUDWATCH_METRICS)

        self._client_factory = client_factory
        self._client: Any | None = None
        self._interval = flush_interval_seconds
        self._start_flusher = start_flusher
        self._lock = threading.Lock()
        self._window = _Window()
        self._started = False
        self._stop = threading.Event()

        _log.info(
            "request_metrics: %s for %s",
            "on" if self.enabled else "off (cloudwatch_metrics gate)",
            service,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.enabled
            or scope.get("path") in self.exclude_paths
        ):
            await self.app(scope, receive, send)
            return

        self._ensure_flusher()
        status = 500  # what the client sees when the app raises before responding
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            # Routing mutates the scope it was handed, so the matched route
            # is here once the app returns. Absent for an unmatched path.
            route = _route_template(scope)
            try:
                self.record(route, status, elapsed_ms)
            except Exception:  # never break a request over a metric
                _log.debug("request_metrics: record failed", exc_info=True)

    def record(self, route: str | None, status: int, elapsed_ms: float) -> None:
        """Add one request to the current window."""
        ms = max(0, round(elapsed_ms))
        with self._lock:
            window = self._window
            window.latency.setdefault(None, Counter())[ms] += 1
            if route in self.routes:
                window.latency.setdefault(route, Counter())[ms] += 1
            if status >= 500:
                window.errors_5xx += 1
            elif status >= 400:
                window.errors_4xx += 1

    def flush(self) -> int:
        """Publish the current window. Returns the number of datums sent.

        Never raises. A failure drops the window rather than holding it for
        the next attempt: a minute's metrics arriving late and mislabeled is
        worse than a gap, which reads as what it is.
        """
        with self._lock:
            window, self._window = self._window, _Window()
        if window.empty:
            return 0

        datums = self._datums(window, datetime.now(UTC))
        try:
            if self._client is None:
                self._client = self._client_factory()
            for i in range(0, len(datums), _MAX_DATUMS_PER_CALL):
                self._client.put_metric_data(
                    Namespace=NAMESPACE,
                    MetricData=datums[i : i + _MAX_DATUMS_PER_CALL],
                )
        except Exception as exc:
            # The type only: a boto error's message can carry request detail.
            _log.warning(
                "request_metrics: publish failed (%s); dropped %d datum(s)",
                type(exc).__name__,
                len(datums),
            )
            return 0
        return len(datums)

    def _datums(self, window: _Window, timestamp: datetime) -> list[dict[str, Any]]:
        service_dim = {"Name": "Service", "Value": self.service}
        datums: list[dict[str, Any]] = []

        for route, counts in window.latency.items():
            dimensions = [service_dim]
            if route is not None:
                dimensions = [service_dim, {"Name": "Route", "Value": route}]
            items = sorted(counts.items())
            for i in range(0, len(items), _MAX_VALUES_PER_DATUM):
                chunk = items[i : i + _MAX_VALUES_PER_DATUM]
                datums.append(
                    {
                        "MetricName": "Latency",
                        "Dimensions": dimensions,
                        "Timestamp": timestamp,
                        "Unit": "Milliseconds",
                        "Values": [float(ms) for ms, _ in chunk],
                        "Counts": [float(n) for _, n in chunk],
                    }
                )

        for name, value in (
            ("Errors5xx", window.errors_5xx),
            ("Errors4xx", window.errors_4xx),
        ):
            datums.append(
                {
                    "MetricName": name,
                    "Dimensions": [service_dim],
                    "Timestamp": timestamp,
                    "Unit": "Count",
                    "Value": float(value),
                }
            )
        return datums

    def _ensure_flusher(self) -> None:
        """Start the publish thread on the first recorded request.

        Lazily, so importing or building the app — in tests, in a migration
        script — starts no thread and reaches for no credentials.
        """
        if self._started or not self._start_flusher:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        thread = threading.Thread(
            target=self._run, name="request-metrics-flush", daemon=True
        )
        thread.start()
        # Publish the last partial minute on a clean shutdown (a Railway
        # redeploy's SIGTERM), rather than losing it with the process.
        atexit.register(self._shutdown)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def _shutdown(self) -> None:
        self._stop.set()
        self.flush()
