"""Where an invocation's time went: working, or waiting — and on what.

Why this exists. A Lambda is billed for every millisecond it is running,
at its full memory size, whether it is computing or sitting on a socket.
CloudWatch's ``REPORT`` line gives the total and nothing else, so it
cannot say whether a 4-minute run was 4 minutes of work or 3 minutes of
waiting for Claude. This module answers that, per invocation, in one log
line.

What it measures, inside :func:`invocation`:

``wall_ms`` / ``cpu_ms``
    Wall-clock time, and CPU time from :func:`time.process_time`. The gap
    between them is time the process spent not running, which is one of
    two things: waiting (on I/O, or a sleep), or ready to run but held
    back by the CPU quota.
``throttled_ms``
    The second of those. Lambda gives a function a share of a vCPU in
    proportion to its memory — one full vCPU at 1,769 MB, so 0.58 at
    1,024 MB — and a CPU-bound stretch at 0.58 takes 1.7 times as long.
    That time is not idle and does not belong in ``idle_pct``. Read from
    the cgroup's ``cpu.stat`` when the sandbox exposes it
    (``throttled_from: "cgroup"``); otherwise estimated from
    ``memory_mb`` as ``cpu_ms × (1/share − 1)`` and capped at the idle
    time no wait accounts for (``throttled_from: "memory"``). The
    estimate is approximate: measured runs throttle somewhat more than
    the share alone predicts. Absent off Lambda, where there is no quota
    to infer.
``idle_pct``
    Waiting, as a share of wall time: wall, less CPU, less throttling.
    The headline number.
``wait``
    That waiting, attributed. Every outgoing HTTP call is timed and filed
    under the service it went to (``google``, ``spotify``, ``anthropic``,
    ``api``, …; an unrecognised host is filed under its hostname), and
    every :func:`time.sleep` under ``sleep`` — the retry backoffs. Each
    entry is ``{"ms": …, "calls": …}``.
``unattributed_ms``
    Idle time no category accounts for: a client this module does not
    hook, a response body read after the call returned, or throttling the
    estimate missed.
``memory_mb``
    The function's memory size, from ``AWS_LAMBDA_FUNCTION_MEMORY_SIZE``.

How it hooks the calls. :func:`install` wraps the HTTP transports the
fleet's clients sit on — ``httpx.Client.send`` (the API, Asana),
``httpx2.Client.send`` (the Anthropic and OpenAI SDKs), ``urllib3``'s
``urlopen`` (requests, so Spotify and gspread; and botocore, so SSM),
``httplib2.Http.request`` (the Google API client) — and ``time.sleep``.
It is opt-in: nothing is wrapped until a handler enters
:func:`invocation`, and outside one the wrappers only add a flag check. Time is counted exclusively, so a call made inside another
(urllib3 following a redirect, say) is not counted twice.

What it emits. One JSON line on stdout when the invocation ends, which
CloudWatch Logs Insights parses into fields with no setup::

    {"timing": {"wall_ms": 241380, "cpu_ms": 2210, "throttled_ms": 1600,
                "throttled_from": "memory", "memory_mb": 1024,
                "idle_pct": 98.4,
                "wait": {"anthropic": {"ms": 236100, "calls": 1}, …},
                "unattributed_ms": 1470, "labels": {"cog": "transcription",
                "mode": "wcs-transcripts"}}}

    filter ispresent(timing.wall_ms)
    | stats sum(timing.wait.anthropic.ms) / 1000 as anthropic_s,
            sum(timing.wait.sleep.ms) / 1000 as sleep_s,
            avg(timing.idle_pct) as idle_pct
      by timing.labels.mode

It is a log line, not a CloudWatch metric, so it costs nothing beyond the
log bytes. Waits on several threads at once each count in full, so with a
thread pool the waits can sum past the wall time.

Usage, in a Lambda handler::

    from mini_app_polis import timing

    with timing.invocation(cog="deejay", mode=mode):
        run(...)
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, SupportsIndex
from urllib.parse import urlsplit

#: Host suffix → category. First match wins; checked longest-first.
_HOST_CATEGORIES: dict[str, str] = {
    "googleapis.com": "google",
    "google.com": "google",
    "spotify.com": "spotify",
    "anthropic.com": "anthropic",
    "openai.com": "openai",
    "asana.com": "asana",
    "amazonaws.com": "aws",
    "github.com": "github",
    "githubusercontent.com": "github",
    "pypi.org": "registry",
    "npmjs.org": "registry",
    "hc-ping.com": "healthchecks",
    "sentry.io": "sentry",
    "kaianolevine.com": "api",
    "railway.app": "api",
    "acoustid.org": "acoustid",
    "musicbrainz.org": "musicbrainz",
}

_lock = threading.Lock()
_local = threading.local()
_installed = False
_active = False
_waits: dict[str, list[float]] = {}  # category → [seconds, calls]
_real_sleep = time.sleep

#: Memory at which Lambda allocates one full vCPU.
_MB_PER_VCPU = 1769

#: cgroup v2, then v1: (path, counter, units per second).
_CGROUP_THROTTLE = (
    ("/sys/fs/cgroup/cpu.stat", "throttled_usec", 1e6),
    ("/sys/fs/cgroup/cpu/cpu.stat", "throttled_time", 1e9),
    ("/sys/fs/cgroup/cpu,cpuacct/cpu.stat", "throttled_time", 1e9),
)


def category_for(host: str | None) -> str:
    """The category a call to ``host`` is filed under."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return "unknown"
    for suffix in sorted(_HOST_CATEGORIES, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            return _HOST_CATEGORIES[suffix]
    return host


def _stack() -> list[list[Any]]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


def _credit(category: str, seconds: float, calls: int) -> None:
    with _lock:
        entry = _waits.setdefault(category, [0.0, 0])
        entry[0] += seconds
        entry[1] += calls


@contextmanager
def waiting(category: str) -> Iterator[None]:
    """Count the time inside this block as waiting on ``category``.

    Exclusive: while a nested ``waiting`` block runs, the enclosing one's
    clock is paused, so each second is counted once. A no-op outside
    :func:`invocation`.
    """
    if not _active:
        yield
        return
    stack = _stack()
    now = time.perf_counter()
    nested_same = bool(stack) and stack[-1][0] == category
    if stack:
        outer = stack[-1]
        _credit(outer[0], now - outer[1], 0)
    entry: list[Any] = [category, now]  # [category, when its clock last resumed]
    stack.append(entry)
    try:
        yield
    finally:
        end = time.perf_counter()
        stack.pop()
        # A retry or redirect inside the same client is one call, not two.
        _credit(category, end - entry[1], 0 if nested_same else 1)
        if stack:
            stack[-1][1] = end


def _timed(
    category_of: Callable[..., str], fn: Callable[..., Any]
) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _active:
            return fn(*args, **kwargs)
        try:
            category = category_of(*args, **kwargs)
        except Exception:  # noqa: BLE001 — timing must never break a call
            category = "unknown"
        with waiting(category):
            return fn(*args, **kwargs)

    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


def _sleep(seconds: float | SupportsIndex) -> None:
    if not _active:
        _real_sleep(seconds)
        return
    with waiting("sleep"):
        _real_sleep(seconds)


def install() -> None:
    """Wrap the HTTP transports and ``time.sleep``. Idempotent.

    :func:`invocation` calls this; call it yourself only to install early.
    A transport that is not installed is skipped.
    """
    global _installed
    if _installed:
        return
    _installed = True
    time.sleep = _sleep

    # httpx carries the API and Asana clients; the Anthropic and OpenAI
    # SDKs moved to its successor, httpx2, which has the same Client.send.
    for name in ("httpx", "httpx2"):
        try:
            client_cls = importlib.import_module(name).Client
        except ImportError:
            continue
        client_cls.send = _timed(
            lambda _self, request, *_a, **_k: category_for(request.url.host),
            client_cls.send,
        )
    try:
        from urllib3.connectionpool import HTTPConnectionPool

        HTTPConnectionPool.urlopen = _timed(  # type: ignore[method-assign]
            lambda self, *_a, **_k: category_for(self.host),
            HTTPConnectionPool.urlopen,
        )
    except ImportError:
        pass
    try:
        import httplib2

        httplib2.Http.request = _timed(
            lambda _self, uri, *_a, **_k: category_for(urlsplit(str(uri)).hostname),
            httplib2.Http.request,
        )
    except ImportError:
        pass


class Invocation:
    """The labels and, once it ends, the totals of one invocation."""

    def __init__(self, labels: dict[str, Any]) -> None:
        self.labels = labels
        self.record: dict[str, Any] = {}

    def label(self, **labels: Any) -> None:
        """Add labels known only partway through (the mode, say)."""
        self.labels.update(labels)


@contextmanager
def invocation(
    *, emit: Callable[[str], None] | None = None, **labels: Any
) -> Iterator[Invocation]:
    """Measure the block and emit one ``{"timing": …}`` line when it ends.

    ``labels`` are carried into the line as ``timing.labels`` — the cog
    and the mode, so Logs Insights can group by them. ``emit`` receives
    the line; the default prints it to stdout, which Lambda sends to
    CloudWatch whatever the logging level. The line is emitted however
    the block ends, and emitting never raises.
    """
    global _active
    install()
    with _lock:
        _waits.clear()
    _local.stack = []
    current = Invocation(dict(labels))
    wall0, cpu0 = time.perf_counter(), time.process_time()
    throttled0 = _cgroup_throttled()
    _active = True
    try:
        yield current
    finally:
        _active = False
        wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
        throttled_end = _cgroup_throttled()
        cgroup_throttled = (
            throttled_end - throttled0
            if throttled0 is not None and throttled_end is not None
            else None
        )
        with _lock:
            waits = {k: (v[0], int(v[1])) for k, v in _waits.items()}
            _waits.clear()
        current.record = _record(
            wall,
            cpu,
            waits,
            current.labels,
            cgroup_throttled=cgroup_throttled,
            memory_mb=_memory_mb(),
        )
        # Timing must never fail the run.
        with contextlib.suppress(Exception):
            (emit or _print)(json.dumps({"timing": current.record}, default=str))


def _cgroup_throttled() -> float | None:
    """Seconds this cgroup has been throttled, or None if not exposed."""
    for path, counter, per_second in _CGROUP_THROTTLE:
        try:
            with open(path) as fh:
                for line in fh:
                    name, _, value = line.partition(" ")
                    if name == counter:
                        return int(value) / per_second
        except (OSError, ValueError):
            continue
    return None


def _memory_mb() -> int | None:
    """The Lambda function's memory size, or None off Lambda."""
    try:
        return int(os.environ["AWS_LAMBDA_FUNCTION_MEMORY_SIZE"])
    except (KeyError, ValueError):
        return None


def _record(
    wall: float,
    cpu: float,
    waits: dict[str, tuple[float, int]],
    labels: dict[str, Any],
    *,
    cgroup_throttled: float | None = None,
    memory_mb: int | None = None,
) -> dict[str, Any]:
    not_running = max(wall - cpu, 0.0)
    attributed = sum(seconds for seconds, _ in waits.values())

    throttled: float | None = None
    throttled_from: str | None = None
    if cgroup_throttled is not None:
        throttled, throttled_from = min(cgroup_throttled, not_running), "cgroup"
    elif memory_mb:
        share = min(memory_mb / _MB_PER_VCPU, 1.0)
        estimate = cpu * (1 / share - 1)
        # Throttling only stretches CPU work; it cannot be time already
        # accounted for as waiting on a call.
        throttled = min(estimate, max(not_running - attributed, 0.0))
        throttled_from = "memory"

    idle = max(not_running - (throttled or 0.0), 0.0)
    record: dict[str, Any] = {
        "wall_ms": round(wall * 1000),
        "cpu_ms": round(cpu * 1000),
    }
    if throttled is not None:
        record["throttled_ms"] = round(throttled * 1000)
        record["throttled_from"] = throttled_from
    if memory_mb:
        record["memory_mb"] = memory_mb
    record.update(
        {
            "idle_pct": round(100 * idle / wall, 1) if wall > 0 else 0.0,
            "wait": {
                k: {"ms": round(seconds * 1000), "calls": calls}
                for k, (seconds, calls) in sorted(waits.items())
            },
            "unattributed_ms": round(max(idle - attributed, 0.0) * 1000),
            "labels": labels,
        }
    )
    return record


def _print(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
