"""Shared pipeline-status reporting for Kaiano cogs.

Each cog (deejay-cog, evaluator-cog, retag-cog, …) runs as one or more
Prefect flows and self-reports the outcome of every run to the Kaiano
API at ``POST /v1/evaluations``. This module centralises that wiring so
no cog has to hand-roll the helpers, hooks, or payload shape.

Two entry points cover the common cases:

- :func:`post_run_finding` — called at the end of a successful flow run
  to emit a single SUCCESS/WARN finding with optional free-form counter
  extras.
- :func:`make_failure_hook` — returns a Prefect ``on_failure`` /
  ``on_crashed`` hook that posts a WARN (Failed) or ERROR (Crashed)
  finding when the flow itself dies.

Both are **best-effort** — they swallow every exception they can so a
broken evaluation post never masks the real failure of the flow it is
reporting on.

Posts are gated by ``production_only=True`` (the default) **and** the
presence of the ``KAIANO_API_BASE_URL`` env var. Local development runs
should pass ``production_only=False`` so they never write to the API
regardless of which env vars are set.

This module deliberately performs Prefect imports lazily so consumers
that don't use Prefect (or test harnesses without it) don't pay the
import cost. ``mini_app_polis`` does not declare Prefect as a runtime
dependency.

Severity classification:

  SUCCESS — run completed end-to-end; no issues a human needs to review.
            Includes "nothing to do" (empty input) and intentional skip
            paths.
  WARN    — run completed but produced results worth a human look
            (e.g. some items failed, some inputs malformed). Also used
            by :func:`make_failure_hook` for Prefect "Failed" state.
  ERROR   — flow-level crash. Emitted only by :func:`make_failure_hook`
            when Prefect reports the run as "Crashed".

The Kaiano API's ``PipelineEvaluationCreate`` schema is the source of
truth for the payload fields; this module sends ``run_id``, ``repo``,
``flow_name``, ``dimension``, ``severity``, ``finding``, and ``source``.
Anything else the API expects must be added here, never tacked on by an
individual cog.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import Any, Literal

from mini_app_polis import logger as logger_mod

_log = logger_mod.get_logger()

Severity = Literal["SUCCESS", "WARN", "ERROR"]
"""Severities a cog may self-report.

The API also accepts ``INFO`` for LLM-evaluator paths, but cogs reporting
their own status don't need it — SUCCESS/WARN/ERROR is the full ladder
of "did the run do its job?".
"""

DEFAULT_DIMENSION = "pipeline_consistency"
"""Default dimension for self-reported findings.

Cogs can override via the ``dimension`` keyword if they want to report
on a different axis (e.g. ``data_quality``, ``freshness``).
"""


def get_prefect_logger() -> Any:
    """Return the Prefect run logger inside a flow, else the module logger.

    Imports Prefect lazily so non-Prefect callers (and test environments
    without Prefect installed) don't pay the import cost. Falls back to
    the mini_app_polis module logger if Prefect isn't available or if we
    aren't inside a flow run context.
    """
    try:
        from prefect import get_run_logger  # lazy import
    except Exception:
        return _log

    try:
        return get_run_logger()
    except Exception:
        return _log


def get_run_id() -> str:
    """Return a stable identifier for the current run.

    Resolution order:

    1. ``prefect.runtime.flow_run.id`` — set when running inside a Prefect
       flow.
    2. ``PREFECT_FLOW_RUN_ID`` env var — set by the Prefect worker
       process.
    3. ``"local-run"`` — fallback for direct invocations and tests.

    Deliberately does **not** consult ``GITHUB_RUN_ID``. GitHub Actions
    is a trigger, not a run identity, in the Kaiano cog ecosystem.
    """
    try:
        from prefect.runtime import flow_run as _flow_run  # lazy import

        rid = getattr(_flow_run, "id", None)
        if rid:
            return str(rid)
    except Exception:
        pass

    env_rid = os.environ.get("PREFECT_FLOW_RUN_ID")
    if env_rid:
        return env_rid

    return "local-run"


def _should_post(production_only: bool) -> bool:
    """Return True iff a self-reported finding should actually be POSTed."""
    if not production_only:
        return False
    return bool(os.environ.get("KAIANO_API_BASE_URL"))


def _nonzero_extras(counters: dict[str, Any]) -> dict[str, Any]:
    """Return ``counters`` with zero/empty/falsy values stripped.

    Used to build the ``k=v`` suffix appended to the finding text — we
    only surface counters that actually have something to say.
    """
    out: dict[str, Any] = {}
    for k, v in counters.items():
        if v is None or v is False:
            continue
        if isinstance(v, int | float) and v == 0:
            continue
        if isinstance(v, list | tuple | dict | str) and len(v) == 0:
            continue
        out[k] = v
    return out


def _merge_extras_into_text(text: str, extras: dict[str, Any]) -> str:
    """Append non-zero ``extras`` as ``k=v`` pairs after ``text``."""
    nz = _nonzero_extras(extras)
    if not nz:
        return text
    suffix = "; ".join(f"{k}={v}" for k, v in sorted(nz.items()))
    return f"{text} {suffix}" if text else suffix


def _post_evaluation(payload: dict[str, Any]) -> None:
    """POST a self-reported finding to ``/v1/evaluations``. Never raises.

    Uses :class:`mini_app_polis.api.KaianoApiClient` so we share the
    Clerk M2M auth path the rest of the cog ecosystem uses.
    """
    logger = get_prefect_logger()
    try:
        from mini_app_polis.api import KaianoApiClient  # local import
    except Exception:
        logger.exception(
            "pipeline_status: Kaiano API client not available; finding not posted"
        )
        return

    try:
        client = KaianoApiClient.from_env()
        client.post("/v1/evaluations", payload)
    except Exception:
        logger.exception(
            "pipeline_status: failed to POST self-reported finding (best-effort)"
        )


def post_run_finding(
    flow_name: str,
    severity: Severity,
    text: str | None = None,
    *,
    repo: str,
    dimension: str = DEFAULT_DIMENSION,
    production_only: bool = True,
    source: str = "flow_inline",
    **extras: Any,
) -> None:
    """Emit exactly one self-reported finding for this run.

    Parameters
    ----------
    flow_name:
        Name of the flow as it appears in Prefect.
    severity:
        One of ``"SUCCESS"``, ``"WARN"``, ``"ERROR"``. Sent verbatim to
        the API.
    text:
        Human-readable finding. If omitted and severity is SUCCESS, a
        default of ``"Run completed successfully."`` is used.
    repo:
        Name of the cog (e.g. ``"deejay-cog"``). Required.
    dimension:
        Evaluation dimension; defaults to ``"pipeline_consistency"``.
    production_only:
        When False, this call is a no-op regardless of env vars — used
        by local-only or WIP flows that should never write to the
        production evaluations table.
    source:
        ``"flow_inline"`` for end-of-flow calls, ``"flow_hook"`` for
        Prefect on_failure/on_crashed hook calls. Free-form otherwise.
    **extras:
        Cog-specific counters or flags. Non-zero values are appended to
        the finding text as ``k=v`` pairs (sorted alphabetically) so
        operators can see them at a glance without the cog having to
        hand-format the string.

    Best-effort: exceptions inside the HTTP layer are logged, never
    raised.
    """
    logger = get_prefect_logger()

    if severity == "SUCCESS" and text is None:
        text = "Run completed successfully."

    text_final = _merge_extras_into_text(text or "", extras).strip()

    if not _should_post(production_only):
        logger.debug(
            "pipeline_status finding suppressed "
            "(repo=%s flow=%s severity=%s production_only=%s)",
            repo,
            flow_name,
            severity,
            production_only,
        )
        return

    if not text_final:
        logger.warning(
            "pipeline_status: refusing to post finding with empty text "
            "(repo=%s flow=%s severity=%s)",
            repo,
            flow_name,
            severity,
        )
        return

    payload = {
        "run_id": get_run_id(),
        "repo": repo,
        "flow_name": flow_name,
        "dimension": dimension,
        "severity": severity,
        "finding": text_final,
        "source": source,
    }
    try:
        _post_evaluation(payload)
    except Exception:
        # Defense in depth: _post_evaluation already handles its own
        # exceptions, but post_run_finding must never propagate to flow
        # code. Logging here is itself best-effort — some test stubs
        # ship logger objects without .exception(), and a crash in the
        # error-reporting path would defeat the whole guarantee.
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: self-reported finding post raised "
                "unexpectedly (should be best-effort)"
            )


def make_failure_hook(
    flow_name: str,
    *,
    repo: str,
    production_only: bool = True,
    dimension: str = DEFAULT_DIMENSION,
) -> Callable[..., None]:
    """Return a Prefect ``on_failure`` / ``on_crashed`` hook.

    The returned hook posts a finding with severity ``WARN`` for the
    ``Failed`` state and ``ERROR`` for ``Crashed``. It always logs the
    failure locally; the API POST is gated the same way as
    :func:`post_run_finding`.

    Parameters
    ----------
    flow_name:
        Name of the flow this hook reports on.
    repo:
        Name of the cog. Required.
    production_only:
        Same semantics as :func:`post_run_finding`.
    dimension:
        Evaluation dimension; defaults to ``"pipeline_consistency"``.

    The returned callable never raises — hooks that raise can mask the
    underlying flow failure.
    """

    def _hook(flow, flow_run, state) -> None:  # noqa: ARG001
        logger = get_prefect_logger()
        try:
            state_name = str(getattr(state, "name", "FAILED"))
            state_type = str(getattr(state, "type", "")).upper()
            severity: Severity = (
                "ERROR"
                if state_type == "CRASHED" or state_name == "Crashed"
                else "WARN"
            )

            logger.error(
                "Flow failure hook fired: repo=%s flow=%s run_id=%s "
                "state=%s production_only=%s",
                repo,
                flow_name,
                get_run_id(),
                state_name,
                production_only,
            )

            post_run_finding(
                flow_name,
                severity,
                text=f"Flow entered {state_name} unexpectedly",
                repo=repo,
                dimension=dimension,
                production_only=production_only,
                source="flow_hook",
            )
        except Exception:
            logger.exception("Flow failure hook failed unexpectedly")

    return _hook


__all__ = [
    "DEFAULT_DIMENSION",
    "Severity",
    "get_prefect_logger",
    "get_run_id",
    "make_failure_hook",
    "post_run_finding",
]
