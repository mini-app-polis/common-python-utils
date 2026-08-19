"""Unit tests for :mod:`mini_app_polis.serve_resilience`.

Covers the three guarantees the module promises:

1. **Correct retry classification** — transient upstream failures
   (429/5xx, network errors) retry; configuration errors (401/403/404)
   fail fast. Retrying a bad API key for 30 minutes would turn a
   two-second config fix into a half-hour outage with no signal.
2. **Exactly one finding on give-up** — one ``source="startup"``,
   ``CRITICAL`` row, then the original exception is re-raised so the
   process exits non-zero and Railway's ON_FAILURE policy restarts it.
3. **No happy-path emission** — a successful ``serve()`` posts nothing.
   ``serve()`` blocks forever when it works, so any success finding
   would be pure deploy-time noise in Pipeline Health.

Prefect is stubbed via ``sys.modules`` rather than imported. The module
under test imports ``prefect.serve`` lazily precisely so that
``mini_app_polis`` need not declare Prefect as a runtime dependency —
these tests pin that property by passing with no Prefect installed.
"""

from __future__ import annotations

import math
import sys
from types import ModuleType
from unittest.mock import patch

import httpx
import pytest
from tenacity import wait_none

import mini_app_polis.pipeline_status as ps
import mini_app_polis.serve_resilience as sr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_error(code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError carrying ``code``."""
    request = httpx.Request("GET", "https://api.prefect.cloud/api/deployments/name")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


class _PrefectHTTPStatusError(httpx.HTTPStatusError):
    """Stand-in for ``prefect.exceptions.PrefectHTTPStatusError``.

    The real class subclasses ``httpx.HTTPStatusError``; this local
    stand-in pins that ``_is_retryable`` classifies Prefect's own wrapper
    through the base-class branch, without this library importing
    Prefect to name it.
    """


def _prefect_status_error(code: int) -> _PrefectHTTPStatusError:
    request = httpx.Request("GET", "https://api.prefect.cloud/api/deployments/name")
    response = httpx.Response(code, request=request)
    return _PrefectHTTPStatusError(f"HTTP {code}", request=request, response=response)


def _noop_serve(*_args, **_kwargs) -> None:
    """A ``serve()`` that returns immediately instead of blocking forever."""
    return None


def _raising_serve(exc: BaseException):
    """Build a ``serve()`` stub that always raises ``exc``."""

    def _serve(*_args, **_kwargs):
        raise exc

    return _serve


def _no_wait(**_kwargs):
    """Backoff factory that never sleeps — swapped in by ``no_backoff``."""
    return wait_none()


def _install_serve(monkeypatch, fn) -> list:
    """Install a stub ``prefect`` module whose ``serve`` is ``fn``.

    Returns the list that records ``(args, kwargs)`` per call.
    """
    calls: list = []

    def _recording_serve(*args, **kwargs):
        calls.append((args, kwargs))
        return fn(*args, **kwargs)

    module = ModuleType("prefect")
    module.serve = _recording_serve  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "prefect", module)
    return calls


@pytest.fixture
def no_backoff(monkeypatch):
    """Collapse the exponential backoff so retry tests run instantly.

    Only the sleep is neutralised — attempt counting, stop conditions,
    and the give-up path are exercised for real.
    """
    monkeypatch.setattr(sr, "wait_exponential", _no_wait)


@pytest.fixture
def api_configured(monkeypatch):
    """Satisfy pipeline_status's posting gate so findings are attempted."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.delenv("PREFECT_FLOW_RUN_ID", raising=False)
    monkeypatch.delenv(sr.MAX_SECONDS_ENV_VAR, raising=False)
    ps._resolve_processor_version.cache_clear()


# ---------------------------------------------------------------------------
# _is_retryable — transient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [408, 429, 500, 501, 502, 503, 504, 507])
def test_is_retryable_true_for_transient_status_codes(code: int) -> None:
    assert sr._is_retryable(_status_error(code)) is True


@pytest.mark.parametrize("code", [520, 521, 522, 523, 524, 525, 527, 529])
def test_is_retryable_true_for_cloudflare_edge_codes(code: int) -> None:
    """Prefect Cloud is Cloudflare-fronted; 522/524 are how outages present.

    An allowlist of {500, 502, 503, 504} fails fast on exactly these,
    then posts a finding claiming the failure was a configuration error.
    That is the July incident's failure mode with a misleading alert
    attached, which is why the classifier uses a >= 500 range.
    """
    assert sr._is_retryable(_status_error(code)) is True


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_is_retryable_true_for_prefect_wrapped_transient(code: int) -> None:
    """PrefectHTTPStatusError subclasses httpx.HTTPStatusError."""
    assert sr._is_retryable(_prefect_status_error(code)) is True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool exhausted"),
        httpx.ReadError("read failed"),
        httpx.WriteError("write failed"),
        httpx.CloseError("close failed"),
        httpx.ProxyError("proxy failed"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=[
        "connect_error",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "read_error",
        "write_error",
        "close_error",
        "proxy_error",
        "remote_protocol",
    ],
)
def test_is_retryable_true_for_network_errors(exc: Exception) -> None:
    assert sr._is_retryable(exc) is True


def test_connect_timeout_is_not_a_connect_error_subclass() -> None:
    """Why ConnectTimeout is listed separately in RETRYABLE_NETWORK_ERRORS.

    httpx files timeouts under TimeoutException and connection failures
    under NetworkError — siblings, not parent/child. Listing only
    ConnectError would fail fast on the most likely symptom of a Prefect
    Cloud incident. If this assertion ever fails, httpx reorganised its
    exception tree and the tuple should be revisited.
    """
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)
    assert issubclass(httpx.ConnectTimeout, httpx.TimeoutException)
    assert issubclass(httpx.ConnectError, httpx.NetworkError)


# ---------------------------------------------------------------------------
# _is_retryable — fail fast
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405, 409, 422])
def test_is_retryable_false_for_client_errors(code: int) -> None:
    """A bad API key or missing deployment is config, not an outage."""
    assert sr._is_retryable(_status_error(code)) is False


@pytest.mark.parametrize("code", [401, 403, 404])
def test_is_retryable_false_for_prefect_wrapped_client_errors(code: int) -> None:
    assert sr._is_retryable(_prefect_status_error(code)) is False


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad config"),
        RuntimeError("something else"),
        httpx.LocalProtocolError("our bug"),
        KeyboardInterrupt(),
    ],
    ids=["value_error", "runtime_error", "local_protocol", "keyboard_interrupt"],
)
def test_is_retryable_false_for_non_transport_errors(exc: BaseException) -> None:
    assert sr._is_retryable(exc) is False


def test_is_retryable_false_when_response_missing() -> None:
    """Defensive: a status error with no usable response is not retryable."""

    class _Headless(httpx.HTTPStatusError):
        def __init__(self) -> None:  # noqa: D107 - test double
            Exception.__init__(self, "no response")

    assert sr._is_retryable(_Headless()) is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_success_calls_serve_once_and_posts_nothing(
    monkeypatch, api_configured
) -> None:
    calls = _install_serve(monkeypatch, _noop_serve)
    with patch.object(ps, "_post_evaluation") as post:
        sr.serve_with_retry("dep-a", repo="deejay-cog")
    assert len(calls) == 1
    post.assert_not_called()


def test_deployments_and_kwargs_forwarded_verbatim(monkeypatch, api_configured) -> None:
    calls = _install_serve(monkeypatch, _noop_serve)
    sr.serve_with_retry(
        "dep-a", "dep-b", repo="deejay-cog", limit=5, pause_on_shutdown=False
    )
    args, kwargs = calls[0]
    assert args == ("dep-a", "dep-b")
    assert kwargs == {"limit": 5, "pause_on_shutdown": False}


def test_transient_then_success_posts_nothing(
    monkeypatch, api_configured, no_backoff
) -> None:
    """A blip that clears within the ceiling must be invisible downstream."""
    state = {"n": 0}

    def _flaky(*_a, **_k):
        state["n"] += 1
        if state["n"] < 3:
            raise _prefect_status_error(503)
        return None

    calls = _install_serve(monkeypatch, _flaky)
    with patch.object(ps, "_post_evaluation") as post:
        sr.serve_with_retry("dep", repo="deejay-cog")
    assert len(calls) == 3
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Give-up — retries exhausted
# ---------------------------------------------------------------------------


def test_giveup_posts_exactly_one_critical_startup_finding(
    monkeypatch, api_configured, no_backoff
) -> None:
    def _always_503(*_a, **_k):
        raise _prefect_status_error(503)

    calls = _install_serve(monkeypatch, _always_503)
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog", max_attempts=4)

    assert len(calls) == 4, "the attempt-count guard must bound the retries"
    assert post.call_count == 1, "exactly one finding, not one per attempt"
    payload = post.call_args.args[0]
    assert payload["severity"] == "CRITICAL"
    assert payload["source"] == "startup"
    assert payload["repo"] == "deejay-cog"
    assert payload["flow_name"] == "startup"
    assert "attempts=4" in payload["finding"]


def test_giveup_reraises_original_exception(
    monkeypatch, api_configured, no_backoff
) -> None:
    """Re-raising is load-bearing: a zero exit gives Railway nothing to restart."""
    sentinel = _prefect_status_error(503)
    _install_serve(monkeypatch, _raising_serve(sentinel))
    with (
        patch.object(ps, "_post_evaluation"),
        pytest.raises(httpx.HTTPStatusError) as excinfo,
    ):
        sr.serve_with_retry("dep", repo="deejay-cog", max_attempts=2)
    assert excinfo.value is sentinel


def test_wall_clock_ceiling_stops_retrying(
    monkeypatch, api_configured, no_backoff
) -> None:
    """The delay ceiling — not the attempt guard — is what ends the loop.

    ``max_attempts`` is left at its default 120 here, so the only thing
    that can stop this is ``stop_after_delay``. Two calls, not one:
    tenacity evaluates ``stop`` *after* an attempt, and the first attempt
    completes faster than the ceiling, so one more attempt happens before
    the elapsed clock trips.

    The practical consequence, worth knowing when reading Railway logs:
    the real worst-case wall clock is the ceiling plus one final backoff
    sleep (≤30s) plus one attempt — not the ceiling exactly.

    The bound is a range, not an exact count: whether the very first stop
    check trips depends on whether that attempt took longer than the
    microsecond ceiling, which a GC pause can decide. What matters — and
    what is asserted — is that the loop ended nowhere near
    ``DEFAULT_MAX_ATTEMPTS``.
    """
    calls = _install_serve(monkeypatch, _raising_serve(_status_error(503)))
    with (
        patch.object(ps, "_post_evaluation"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog", max_seconds=0.0001)
    assert 1 <= len(calls) <= 2
    assert len(calls) < sr.DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Give-up — non-retryable
# ---------------------------------------------------------------------------


def test_non_retryable_fails_fast_and_still_posts(
    monkeypatch, api_configured, no_backoff
) -> None:
    """401 must not be retried — but the process still dies, so still report."""
    calls = _install_serve(monkeypatch, _raising_serve(_prefect_status_error(401)))
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog", max_attempts=8)

    assert len(calls) == 1, "a config error must not burn the retry ceiling"
    assert post.call_count == 1
    payload = post.call_args.args[0]
    assert payload["severity"] == "CRITICAL"
    assert payload["source"] == "startup"
    assert "non-retryable" in payload["finding"]
    assert "PREFECT_API_KEY" in payload["suggestion"]


# ---------------------------------------------------------------------------
# Best-effort reporting
# ---------------------------------------------------------------------------


def test_finding_post_failure_does_not_mask_startup_failure(
    monkeypatch, api_configured, no_backoff
) -> None:
    """If the API is down too, the original error still reaches the exit code."""
    sentinel = _status_error(503)
    _install_serve(monkeypatch, _raising_serve(sentinel))
    with (
        patch.object(sr, "post_run_finding", side_effect=RuntimeError("api down")),
        pytest.raises(httpx.HTTPStatusError) as excinfo,
    ):
        sr.serve_with_retry("dep", repo="deejay-cog", max_attempts=2)
    assert excinfo.value is sentinel


def test_production_only_false_suppresses_post_but_still_raises(
    monkeypatch, api_configured, no_backoff
) -> None:
    _install_serve(monkeypatch, _raising_serve(_status_error(503)))
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry(
            "dep", repo="deejay-cog", production_only=False, max_attempts=2
        )
    post.assert_not_called()


def test_flow_name_override_reaches_the_finding(
    monkeypatch, api_configured, no_backoff
) -> None:
    _install_serve(monkeypatch, _raising_serve(_status_error(503)))
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry(
            "dep", repo="deejay-cog", flow_name="deejay-cog-boot", max_attempts=2
        )
    assert post.call_args.args[0]["flow_name"] == "deejay-cog-boot"


# ---------------------------------------------------------------------------
# _resolve_max_seconds
# ---------------------------------------------------------------------------


def test_max_seconds_defaults(monkeypatch) -> None:
    monkeypatch.delenv(sr.MAX_SECONDS_ENV_VAR, raising=False)
    assert sr._resolve_max_seconds(None) == sr.DEFAULT_MAX_SECONDS


def test_max_seconds_env_override(monkeypatch) -> None:
    monkeypatch.setenv(sr.MAX_SECONDS_ENV_VAR, "600")
    assert sr._resolve_max_seconds(None) == 600.0


def test_max_seconds_argument_beats_env(monkeypatch) -> None:
    monkeypatch.setenv(sr.MAX_SECONDS_ENV_VAR, "600")
    assert sr._resolve_max_seconds(90) == 90.0


@pytest.mark.parametrize("raw", ["banana", "", "0", "-5"])
def test_max_seconds_bad_env_falls_back_to_default(monkeypatch, raw: str) -> None:
    """A typo in a Railway variable must not become a second way to crash."""
    monkeypatch.setenv(sr.MAX_SECONDS_ENV_VAR, raw)
    assert sr._resolve_max_seconds(None) == sr.DEFAULT_MAX_SECONDS


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_lazy_package_export_does_not_import_module_eagerly() -> None:
    """`from mini_app_polis import serve_with_retry` resolves via __getattr__."""
    import mini_app_polis

    assert mini_app_polis.serve_with_retry is sr.serve_with_retry
    assert "serve_with_retry" in dir(mini_app_polis)


def test_unknown_package_attribute_still_raises() -> None:
    import mini_app_polis

    with pytest.raises(AttributeError):
        _ = mini_app_polis.definitely_not_a_thing


def test_keyboard_interrupt_propagates_without_posting(
    monkeypatch, api_configured, no_backoff
) -> None:
    """Ctrl-C / SIGINT is an operator action, not an incident.

    tenacity catches BaseException, so KeyboardInterrupt reaches the
    retry machinery, is classified non-retryable, and is re-raised. The
    give-up handler catches ``Exception``, not ``BaseException``, so it
    is deliberately skipped — the interrupt passes straight through to
    the cog's own ``except KeyboardInterrupt: sys.exit(0)`` guard with no
    CRITICAL finding emitted.

    Widening that handler to ``BaseException`` would make every graceful
    shutdown post a fleet-down alert.
    """
    _install_serve(monkeypatch, _raising_serve(KeyboardInterrupt()))
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(KeyboardInterrupt),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog")
    post.assert_not_called()


def test_local_protocol_error_stays_non_retryable() -> None:
    """LocalProtocolError means we built a bad request — our bug, not theirs.

    It lives under ProtocolError alongside RemoteProtocolError, so
    widening to the whole ProtocolError branch would have swept it in.
    """
    assert sr._is_retryable(httpx.LocalProtocolError("our bug")) is False


# ---------------------------------------------------------------------------
# Mixed failure sequences
# ---------------------------------------------------------------------------


def test_outage_ending_in_config_error_reports_as_exhausted(
    monkeypatch, api_configured, no_backoff
) -> None:
    """A real outage can end on a non-retryable error. Say "exhausted", not "fast".

    Scenario: Prefect Cloud returns 503 twice, then a 401 as its auth
    service comes back wrong. Classifying the *final* exception alone
    would emit "failed fast without retrying ... attempts=3" — which is
    self-contradictory — and send the on-call to check API keys during an
    incident that was not their fault.
    """
    state = {"n": 0}

    def _degrading(*_a, **_k):
        state["n"] += 1
        raise _prefect_status_error(503 if state["n"] < 3 else 401)

    calls = _install_serve(monkeypatch, _degrading)
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog")

    assert len(calls) == 3
    finding = post.call_args.args[0]["finding"]
    assert "retries exhausted" in finding
    assert "failed fast without retrying" not in finding
    assert "attempts=3" in finding
    # The final error's class is still surfaced so triage isn't misled
    # in the other direction either.
    assert "non-retryable" in finding


def test_immediate_config_error_still_reports_as_fail_fast(
    monkeypatch, api_configured, no_backoff
) -> None:
    """The converse: no retries happened, so "failed fast" is accurate."""
    _install_serve(monkeypatch, _raising_serve(_prefect_status_error(403)))
    with (
        patch.object(ps, "_post_evaluation") as post,
        pytest.raises(httpx.HTTPStatusError),
    ):
        sr.serve_with_retry("dep", repo="deejay-cog")
    finding = post.call_args.args[0]["finding"]
    assert "failed fast without retrying" in finding
    assert "retries exhausted" not in finding


# ---------------------------------------------------------------------------
# Non-finite ceilings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_non_finite_ceiling_falls_back_to_default(monkeypatch, raw: str) -> None:
    """NaN/inf must not become the ceiling — they disable it entirely.

    ``float("nan") <= 0`` is False, so a bare non-positive guard lets NaN
    through, and ``stop_after_delay(nan)`` compares ``elapsed >= nan``,
    which is False for every elapsed value. The wall-clock ceiling would
    silently vanish and only the attempt guard would bound the loop —
    roughly doubling the layer-1 window the coverage math assumes.
    """
    monkeypatch.setenv(sr.MAX_SECONDS_ENV_VAR, raw)
    resolved = sr._resolve_max_seconds(None)
    assert resolved == sr.DEFAULT_MAX_SECONDS
    assert math.isfinite(resolved)


def test_non_finite_ceiling_argument_also_rejected(monkeypatch) -> None:
    """The env var is the likely source, but the argument path is guarded too."""
    monkeypatch.delenv(sr.MAX_SECONDS_ENV_VAR, raising=False)
    assert sr._resolve_max_seconds(float("nan")) == sr.DEFAULT_MAX_SECONDS
    assert sr._resolve_max_seconds(float("inf")) == sr.DEFAULT_MAX_SECONDS
