"""Resilient Prefect ``serve()`` startup for Kaiano cogs.

Every ``serve()``-based cog in the ecosystem shares the same
scaffolding (ecosystem-standards CD-015, deejay-cog ADR-001): ``main()``
builds one or more deployments and hands them to ``prefect.serve()``,
which blocks for the life of the Railway service.

The problem this module solves is that ``prefect.serve()`` is **not**
resilient at its own front door. Before the runner loop begins, it makes
a blocking, fail-fast HTTP call to Prefect Cloud to register/resolve each
deployment (``read_deployment_by_name``). A transient upstream error on
that single call propagates straight out of ``main()`` and the process
exits non-zero.

On 2026-07-22 a seconds-long Prefect Cloud ``503`` on that endpoint took
down all four ``serve()``-based cogs at once. Because the scaffolding is
shared, it was one single point of failure hit four times, not four
coincidences. Worse, it was **silent**: each flow's ``on_failure`` /
``on_crashed`` hooks are correctly wired, but those hooks attach to
*flow runs*, and this crash happens before any flow run exists. No hook
fired, no finding was posted, and the fleet was down with no signal.

:func:`serve_with_retry` is layer 1 of a two-layer defense:

  **Layer 1 (here)** — ride out the blip in-process. Retry the
  registration call with bounded exponential backoff for a wall-clock
  ceiling (default 30 minutes), then give up.

  **Layer 2 (Railway)** — ``restartPolicyType: ON_FAILURE`` with
  ``restartPolicyMaxRetries``, declared in each cog's version-controlled
  ``railway.json`` (ecosystem-standards CD-017). Recovers even when
  retries are exhausted, or when the failure is something retries can't
  fix (a wedged event loop, leaked in-process state).

The two layers multiply: total outage coverage is roughly
``layer-1 ceiling × layer-2 max retries``. The default 30-minute ceiling
against 10 Railway restarts covers a multi-hour Prefect Cloud incident
without human intervention.

On give-up, this module POSTs exactly one ``source="startup"``,
``CRITICAL`` finding via :func:`mini_app_polis.pipeline_status.post_run_finding`
and then re-raises, so the process still exits non-zero and Railway
still restarts it. That finding is the signal the July incident was
missing.

Usage::

    from mini_app_polis.serve_resilience import serve_with_retry

    serve_with_retry(
        router.to_deployment(name="deejay-cog"),
        repo="deejay-cog",
    )

Design constraints worth knowing before editing this module:

- **No module-scope Prefect import.** ``mini_app_polis`` does not
  declare Prefect as a runtime dependency (see
  :mod:`mini_app_polis.pipeline_status`), so ``prefect.serve`` is
  imported lazily inside the function and retryability is classified on
  ``httpx`` types alone. ``prefect.exceptions.PrefectHTTPStatusError``
  subclasses :class:`httpx.HTTPStatusError`, so the status-code branch
  covers it without naming it. The unit tests run with no Prefect
  installed.

- **No happy-path finding.** ``serve()`` blocks forever when it
  succeeds, so nothing after the call runs. A "started successfully"
  finding would only ever fire on the failure branch anyway, and on
  every deploy it would be pure noise in Pipeline Health.

- **Fail fast on 4xx, except 408/429.** A ``401``/``403`` (bad
  ``PREFECT_API_KEY``) or ``404`` (deployment or work-queue gone) is a
  configuration error. Retrying it burns the ceiling and delays the real
  signal by 30 minutes.

- **Retry all 5xx, not an allowlist.** Prefect Cloud sits behind
  Cloudflare, which emits 520–529 at its edge when an origin is
  degraded. An allowlist of ``{500, 502, 503, 504}`` would fail fast on
  the most common real presentation of a Prefect Cloud incident.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
)

from mini_app_polis.pipeline_status import get_prefect_logger, post_run_finding

DEFAULT_MAX_SECONDS = 1800.0
"""Wall-clock ceiling, in seconds, for in-process registration retries.

30 minutes. Chosen so that layer 1 × layer 2 (10 Railway restarts)
covers a multi-hour Prefect Cloud incident. The ceiling is expressed in
wall-clock rather than attempt count deliberately: ``stop_after_attempt``
silently changes meaning whenever the backoff curve is tuned, whereas
"ride out N minutes" is the property operators actually reason about.

Override per-process with the :data:`MAX_SECONDS_ENV_VAR` env var — no
library release or cog redeploy required.
"""

DEFAULT_MAX_ATTEMPTS = 120
"""Runaway guard, not the primary ceiling.

With :data:`DEFAULT_MAX_SECONDS` and the default backoff curve (capped
at 30s per sleep), the delay ceiling is reached in roughly 65 attempts.
This bound only fires if ``serve()`` starts failing instantly and
repeatedly in a way that outpaces the clock.
"""

MAX_SECONDS_ENV_VAR = "SERVE_RETRY_MAX_SECONDS"
"""Env var that overrides :data:`DEFAULT_MAX_SECONDS`.

Precedence is: explicit ``max_seconds=`` argument > env var > default.
An unparseable or non-positive value is ignored with a warning rather
than crashing the startup path — a typo in a Railway variable must not
be a second way to take the fleet down.
"""

STARTUP_SOURCE = "startup"
"""``source`` value on findings emitted from the pre-flow lifecycle.

Distinguishes these rows from ``flow_inline`` (end-of-run self-reports)
and ``flow_hook`` (Prefect on_failure/on_crashed hooks) in Pipeline
Health. A ``source="startup"`` row means: no flow run exists, the cog
process itself failed.
"""

RETRYABLE_STATUS_FLOOR = 500
"""Every status at or above this is treated as transient.

A range rather than an allowlist of ``{500, 502, 503, 504}``. Prefect
Cloud sits behind Cloudflare, whose edge emits 520–529 (``522 Connection
Timed Out``, ``524 A Timeout Occurred``) when an origin is degraded —
the single most common way a Prefect Cloud incident actually presents at
the client. An allowlist would fail fast on exactly the symptom this
module exists to survive, and would then post a finding asserting "this
is a configuration error, retrying would not have helped".

The cost of the range is that a genuine server-side ``501 Not
Implemented`` is retried for the full ceiling before reporting. That is
a Prefect-side defect we cannot fix by failing fast anyway, so the
trade is worth it.
"""

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429})
"""Sub-500 statuses also treated as transient.

``408 Request Timeout`` and ``429 Too Many Requests`` are the only 4xx
codes that mean "try again", not "your request is wrong". Every other
4xx — notably 401, 403, and 404 — fails fast as a configuration error.
"""

RETRYABLE_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
)
"""Network-level ``httpx`` errors treated as transient.

httpx splits transport failures beneath ``TransportError`` into
``TimeoutException`` (Connect/Read/Write/Pool), ``NetworkError``
(Connect/Read/Write/Close), ``ProtocolError`` (Local/Remote), and a
standalone ``ProxyError``. Note ``ConnectTimeout`` is **not** a subclass
of ``ConnectError`` — they live in different branches — so naming
individual leaf classes reliably misses siblings that mean the same
thing operationally. Whole branches are taken instead.

``ProxyError`` is included even though a misconfigured proxy is
arguably a config error, because it is indistinguishable at this layer
from a transient proxy failure, and the cost of guessing wrong in the
availability direction (a 30-minute delay before an accurate finding)
is smaller than in the other direction (the cog is down and the finding
says it was our config).

From ``ProtocolError`` only ``RemoteProtocolError`` is taken.
``LocalProtocolError`` means we built a malformed request — a bug on our
side, and retrying a local bug for 30 minutes just delays the finding.
"""


def _is_retryable(exc: BaseException) -> bool:
    """Return True iff ``exc`` is a transient failure worth retrying.

    Transient means: a network-level error from
    :data:`RETRYABLE_NETWORK_ERRORS`, or an HTTP status error whose
    status is at or above :data:`RETRYABLE_STATUS_FLOOR` or listed in
    :data:`RETRYABLE_STATUS_CODES`.

    ``prefect.exceptions.PrefectHTTPStatusError`` subclasses
    :class:`httpx.HTTPStatusError`, so Prefect's own wrapper is matched
    by the status branch without this module importing Prefect.

    Anything else — including 4xx client errors other than 408/429, and
    any non-HTTP exception — is **not** retryable. A bad API key or a
    deleted deployment is a configuration error; retrying it wastes the
    ceiling and delays the operator signal.

    Never raises: a status error with no usable response yields ``None``
    and falls through to ``False``.
    """
    if isinstance(exc, RETRYABLE_NETWORK_ERRORS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            return False
        return status >= RETRYABLE_STATUS_FLOOR or status in RETRYABLE_STATUS_CODES
    return False


def _resolve_max_seconds(max_seconds: float | None) -> float:
    """Resolve the wall-clock ceiling from argument, env var, or default.

    Precedence: explicit argument > :data:`MAX_SECONDS_ENV_VAR` >
    :data:`DEFAULT_MAX_SECONDS`. A malformed, non-finite, or non-positive
    value from *either* source is logged and ignored — a typo in a
    Railway variable must not become a second way to take a cog down at
    startup, and the argument path gets the same guard because it is the
    one a future caller is most likely to wire to a config field.
    """
    if max_seconds is not None:
        return _validated(max_seconds, source="max_seconds argument")

    raw = os.environ.get(MAX_SECONDS_ENV_VAR)
    if not raw:
        return DEFAULT_MAX_SECONDS

    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        get_prefect_logger().warning(
            "serve_resilience: ignoring unparseable %s=%r; using default %ss",
            MAX_SECONDS_ENV_VAR,
            raw,
            DEFAULT_MAX_SECONDS,
        )
        return DEFAULT_MAX_SECONDS

    return _validated(parsed, source=MAX_SECONDS_ENV_VAR)


def _validated(value: float, *, source: str) -> float:
    """Return ``value`` if it is a usable ceiling, else the default.

    NaN and infinity must be rejected explicitly, not just via ``<= 0``.
    ``float("nan") <= 0`` is ``False``, so NaN would slip through as the
    ceiling — and ``stop_after_delay(nan)`` compares ``elapsed >= nan``,
    which is ``False`` for every elapsed value. The wall-clock ceiling
    would silently vanish, leaving only the attempt guard to bound the
    loop and roughly doubling the layer-1 window the coverage math in
    ADR-006 assumes.
    """
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        as_float = float("nan")

    if not math.isfinite(as_float) or as_float <= 0:
        get_prefect_logger().warning(
            "serve_resilience: ignoring non-finite or non-positive %s=%r; "
            "using default %ss",
            source,
            value,
            DEFAULT_MAX_SECONDS,
        )
        return DEFAULT_MAX_SECONDS

    return as_float


def _make_before_sleep(ceiling: float) -> Any:
    """Build a tenacity ``before_sleep`` callback that logs each retry.

    Every retry is logged at WARNING with the attempt number, the elapsed
    wall-clock against the ceiling, and the underlying error, so the
    Railway log is a readable timeline of the outage rather than silence
    followed by a crash.
    """

    def _before_sleep(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        get_prefect_logger().warning(
            "serve_resilience: Prefect deployment registration failed "
            "(attempt %s, %.0fs/%.0fs elapsed); retrying in %.0fs. Error: %s: %s",
            retry_state.attempt_number,
            retry_state.seconds_since_start or 0.0,
            ceiling,
            getattr(retry_state.next_action, "sleep", 0.0),
            type(exc).__name__ if exc is not None else "unknown",
            exc,
        )

    return _before_sleep


def _post_startup_failure_finding(
    *,
    repo: str,
    flow_name: str,
    exc: BaseException,
    attempts: int,
    elapsed: float,
    retryable: bool,
    production_only: bool,
) -> None:
    """POST one CRITICAL ``source="startup"`` finding. Never raises.

    :func:`mini_app_polis.pipeline_status.post_run_finding` is already
    best-effort, but this wrapper adds a second guard: the *only* job
    left on this code path is to re-raise so the process exits and
    Railway restarts it. A failure in the reporting path must not
    interfere with that.

    Note the ``run_id`` on this finding resolves to ``"local-run"`` —
    there is no Prefect flow run to attribute it to, which is precisely
    the condition being reported. ``source="startup"`` is what
    disambiguates it in Pipeline Health.

    The "config error" wording is chosen on whether retries *actually
    happened*, not on how the final exception classifies. Those differ:
    a real outage can end in a non-retryable error (Prefect Cloud returns
    503s for two minutes, then a 401 as its auth service comes back
    wrong). Keying on ``retryable`` alone would emit "failed fast without
    retrying ... attempts=3" — self-contradictory — and send the on-call
    to check API keys during an incident that was not their fault.
    """
    retried = attempts > 1
    if retryable or retried:
        reason = f"retries exhausted after {attempts} attempts over {elapsed:.0f}s"
        if not retryable:
            reason += (
                f"; the final error ({type(exc).__name__}) was itself non-retryable"
            )
        suggestion = (
            "Check the Prefect Cloud status page. Railway's ON_FAILURE restart "
            "policy will restart the service; if restarts are also exhausted, "
            "redeploy manually once Prefect Cloud is healthy. To ride out a "
            f"longer outage in-process, raise {MAX_SECONDS_ENV_VAR}. If the "
            "final error was a 401/403/404, also verify PREFECT_API_KEY and "
            "that the deployment still exists — an outage can surface a "
            "config problem on the way back up."
        )
    else:
        reason = "non-retryable error, failed fast without retrying"
        suggestion = (
            "This is a configuration error, not an outage — retrying would not "
            "have helped. Verify PREFECT_API_KEY, PREFECT_API_URL, and that the "
            "deployment and work queue still exist in Prefect Cloud."
        )

    try:
        post_run_finding(
            flow_name,
            "CRITICAL",
            text=(
                f"Prefect deployment registration failed at startup ({reason}); "
                f"the process is exiting and no flow will run. "
                f"Last error: {type(exc).__name__}: {exc}"
            ),
            repo=repo,
            production_only=production_only,
            source=STARTUP_SOURCE,
            suggestion=suggestion,
            attempts=attempts,
            elapsed_seconds=round(elapsed),
        )
    except Exception:
        get_prefect_logger().exception(
            "serve_resilience: failed to post startup finding (best-effort); "
            "re-raising the original startup failure regardless"
        )


def serve_with_retry(
    *deployments: Any,
    repo: str,
    flow_name: str = STARTUP_SOURCE,
    production_only: bool = True,
    max_seconds: float | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    **serve_kwargs: Any,
) -> None:
    """Call ``prefect.serve(*deployments)`` with startup-failure resilience.

    Drop-in replacement for ``prefect.serve()`` in a cog's ``main()``.
    On success it blocks for the life of the process exactly as
    ``serve()`` does. On a transient failure during deployment
    registration it retries with bounded exponential backoff. On give-up
    it posts one CRITICAL finding and re-raises so the process exits
    non-zero and Railway's restart policy takes over.

    Parameters
    ----------
    *deployments:
        Runner deployments, passed through to ``prefect.serve()``
        unchanged (e.g. ``flow.to_deployment(name="deejay-cog")``).
    repo:
        Name of the cog, e.g. ``"deejay-cog"``. Required and
        keyword-only. Passed explicitly rather than inferred because
        this helper is shared across the fleet and cannot depend on any
        one cog's ``_pipeline_eval`` shim. Must match the cog's
        ``pyproject.toml`` ``[project] name`` for version stamping to
        resolve.
    flow_name:
        ``flow_name`` on the emitted finding. Defaults to ``"startup"``.
        There is no flow at this point in the lifecycle — the value is a
        Pipeline Health label, not a Prefect reference.
    production_only:
        Same gating semantics as
        :func:`mini_app_polis.pipeline_status.post_run_finding`: when
        ``False``, the finding is never POSTed regardless of env vars.
        Retry behaviour is unaffected.
    max_seconds:
        Wall-clock retry ceiling. Defaults to the
        :data:`MAX_SECONDS_ENV_VAR` env var, then to
        :data:`DEFAULT_MAX_SECONDS` (1800s / 30 min).
    max_attempts:
        Runaway guard on attempt count. See :data:`DEFAULT_MAX_ATTEMPTS`;
        the wall-clock ceiling is the operative bound in practice.
    **serve_kwargs:
        Forwarded verbatim to ``prefect.serve()`` (``limit``,
        ``pause_on_shutdown``, ``print_starting_message``, …).

    Raises
    ------
    Exception
        Whatever ``prefect.serve()`` raised, re-raised unchanged once
        retries are exhausted or the error is classified non-retryable.
        Re-raising is load-bearing: a zero exit code would leave Railway
        with nothing to restart.
    """
    from prefect import serve  # lazy — Prefect is not a library dependency

    ceiling = _resolve_max_seconds(max_seconds)
    logger = get_prefect_logger()
    logger.info(
        "serve_resilience: registering %d deployment(s) for %s "
        "(retry ceiling %.0fs, max %d attempts)",
        len(deployments),
        repo,
        ceiling,
        max_attempts,
    )

    retryer = Retrying(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=(stop_after_delay(ceiling) | stop_after_attempt(max_attempts)),
        before_sleep=_make_before_sleep(ceiling),
        reraise=True,
    )

    started = time.monotonic()
    try:
        retryer(serve, *deployments, **serve_kwargs)
    except Exception as exc:
        elapsed = time.monotonic() - started
        attempts = int(retryer.statistics.get("attempt_number", 1) or 1)
        retryable = _is_retryable(exc)
        logger.error(
            "serve_resilience: giving up on Prefect deployment registration "
            "for %s after %d attempt(s) over %.0fs (retryable=%s); exiting so "
            "Railway can restart. Error: %s: %s",
            repo,
            attempts,
            elapsed,
            retryable,
            type(exc).__name__,
            exc,
        )
        _post_startup_failure_finding(
            repo=repo,
            flow_name=flow_name,
            exc=exc,
            attempts=attempts,
            elapsed=elapsed,
            retryable=retryable,
            production_only=production_only,
        )
        raise


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_SECONDS",
    "MAX_SECONDS_ENV_VAR",
    "RETRYABLE_NETWORK_ERRORS",
    "RETRYABLE_STATUS_CODES",
    "RETRYABLE_STATUS_FLOOR",
    "STARTUP_SOURCE",
    "serve_with_retry",
]
