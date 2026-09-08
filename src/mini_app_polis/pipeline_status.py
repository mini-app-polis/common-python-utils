"""Shared pipeline-status reporting for Kaiano cogs.

Each cog (deejay-cog, evaluator-cog, retag-cog, …) runs as one or more
Prefect flows and reports what happened. This module centralises that
wiring so no cog has to hand-roll the helpers, hooks, or payload shapes.

**Two sinks, because there are two different things being reported.**

:func:`post_findings` → ``POST /v1/evaluations``. Graded findings: what
an evaluator judged about a repo against a revision of the standards
catalog. These are records. They are compared, counted and burned down
over time, so they are written to a table and stay there.

:func:`post_run_finding` and :func:`make_failure_hook` → ``POST
/v1/notify``. Run status: this flow finished, this flow crashed. Nothing
is persisted. It used to travel the findings path, which is why the API
had to null out ``standards_version`` on those rows to stop Pipeline
Health claiming they had been evaluated against something — nothing
graded "the flow crashed" against anything.

The two no longer share a funnel. Sharing one is what let run telemetry
into the evaluations table in the first place, and a single change to
"the sink" then moves both.

What run status gives up by not being written down: "did it run" is
answered by Healthchecks.io, which fires on absence rather than on
success, and "what went wrong" by the message in the channel and the
structured log beside it. Counters carried on WARN reports (items
processed, files failed) accumulate nowhere, so "how many failed last
month" has no source. Adding a record back later is additive — the
notification is a fan-out, not a store.

Two entry points cover the common cases:

- :func:`post_run_finding` — called at the end of a flow run to report a
  single SUCCESS/WARN outcome with optional free-form counter extras.
- :func:`make_failure_hook` — returns a Prefect ``on_failure`` /
  ``on_crashed`` hook that reports WARN (Failed) or ERROR (Crashed)
  when the flow itself dies.

Both are **best-effort** — they swallow every exception they can so a
broken notification never masks the real failure of the flow it is
reporting on. Best-effort means never raising; it does not mean claiming
success. Both return a :class:`DeliveryReport` counting what was
actually delivered, and delivery failures go to Sentry when the host
process has it initialised. A caller that logs "reported N" from the
length of what it handed over is the instrument that showed green
through the September outage.

Posts are gated by ``production_only=True`` (the default) **and** the
presence of the ``KAIANO_API_BASE_URL`` env var. Local development runs
should pass ``production_only=False`` so they never write to the API
regardless of which env vars are set.

This module deliberately performs Prefect imports lazily so consumers
that don't use Prefect (or test harnesses without it) don't pay the
import cost. ``mini_app_polis`` does not declare Prefect as a runtime
dependency.

Severity classification:

  SUCCESS  — run completed end-to-end; no issues a human needs to
             review. Includes "nothing to do" (empty input) and
             intentional skip paths.
  WARN     — run completed but produced results worth a human look
             (e.g. some items failed, some inputs malformed). Also used
             by :func:`make_failure_hook` for Prefect "Failed" state.
  ERROR    — flow-level crash. Emitted only by :func:`make_failure_hook`
             when Prefect reports the run as "Crashed".
  CRITICAL — process-level failure with no flow run at all: the cog
             could not register its deployments and is exiting. Emitted
             only by :mod:`mini_app_polis.serve_resilience` with
             ``source="startup"``. See the :data:`Severity` docstring
             for why this is deliberately narrow.

The Kaiano API's ``PipelineEvaluationCreate`` schema is the source of
truth for the findings payload; :func:`post_findings` sends ``run_id``,
``repo``, ``flow_name``, ``dimension``, ``severity``, ``finding`` and
``source``. Run-status messages are built in :func:`_build_message` and
sent through :meth:`mini_app_polis.api.KaianoApiClient.notify`, which
owns that path and body shape.

Both build their client with ``machine_name=repo``, so the API's audit
trail names which cog called rather than only that one did — the cog's
name, its distribution name and its machine name are the same string by
convention, so there is nothing to keep in step.

``SUCCESS`` run reports are logged and not sent; see
:data:`NOTIFY_SEVERITIES`. Findings are never suppressed by severity: a
SUCCESS finding is a graded result and belongs in the table.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, TypedDict

from mini_app_polis import logger as logger_mod

_log = logger_mod.get_logger()

Severity = Literal["SUCCESS", "WARN", "ERROR", "CRITICAL"]
"""Severities a cog may self-report.

The Kaiano API accepts five severities in ``/v1/evaluations``:
``CRITICAL``, ``ERROR``, ``WARN``, ``INFO``, and ``SUCCESS``. This
library exposes the four that map cleanly to "did this cog do its
job?" — the question every self-reporting cog is answering:

  SUCCESS  — run completed end-to-end; nothing for a human to review.
  WARN     — run completed but produced something worth a look.
  ERROR    — flow itself crashed or terminally failed.
  CRITICAL — the cog process itself could not start or stay up; no
             flow run exists to report on. Emitted only from the
             pre-flow lifecycle (see :mod:`mini_app_polis.serve_resilience`).

``INFO`` is intentionally absent from this enum. It belongs to the
LLM-evaluator and webhook paths, where a neutral observation ("flow
entered Running state", "config snapshot recorded") is a useful
signal. For a self-report at end-of-run there is no informational
outcome distinct from SUCCESS — a clean "nothing to do" run is still
a success. Collapsing INFO into SUCCESS keeps the cog's mental model
"did I do my job?" rather than "what category of event was this?".

**On CRITICAL.** This enum originally excluded CRITICAL, on the
reasoning that it is reserved for cross-cog signals no single *flow
run* can authoritatively raise, and that "a cog that is critically
broken typically can't reach this code path at all — it died before
reporting". That reasoning was correct for its scope and is preserved
here, because it still governs how CRITICAL may be used.

The July 2026 fleet-down incident produced the exception the original
note anticipated. A transient Prefect Cloud 503 on the deployment-
registration endpoint killed all four ``serve()``-based cogs at
startup. The crash happened *before any flow run existed*, so the
``on_failure`` / ``on_crashed`` hooks — which attach to flow runs —
could not fire, and the fleet went down silently. With
:func:`mini_app_polis.serve_resilience.serve_with_retry`, that path is
now reachable: the process is alive enough to POST one finding on its
way out. "All four cogs are down and no flow will ever run" is exactly
the cross-cog, fleet-wide signal CRITICAL was reserved for.

CRITICAL therefore remains **narrowly scoped**, and the original
prohibition still stands for everything else:

- CRITICAL is for the **pre-flow process lifecycle only** — the cog
  cannot register, cannot start, and is exiting. It always carries
  ``source="startup"``.
- CRITICAL is **never** "ERROR but I really mean it". A flow run that
  crashed is ERROR, no matter how bad the blast radius. Using CRITICAL
  for a flow-run outcome dilutes the one signal that means "nothing is
  running at all".

If a future cog has a concrete need for INFO as a self-report (not an
LLM-evaluator finding), expand this Literal and extend the regression
tests in ``test_pipeline_status.py``. The API already accepts the
string, so the change is library-only.
"""

#: Severities that reach the notification channel. SUCCESS is logged and
#: goes no further: a fleet announcing "ran fine" on every schedule is the
#: noise problem this path exists to avoid, and Healthchecks.io already
#: answers "did it run" by firing on absence rather than on success.
NOTIFY_SEVERITIES: frozenset[str] = frozenset({"WARN", "ERROR", "CRITICAL"})

#: Message colour per severity, so the channel is scannable without reading.
_SEVERITY_COLORS: dict[str, int] = {
    "SUCCESS": 0x2EA043,
    "WARN": 0xD29922,
    "ERROR": 0xDA3633,
    "CRITICAL": 0x8B0000,
}
_DEFAULT_COLOR = 0x58A6FF


@dataclass(frozen=True)
class DeliveryReport:
    """What actually happened to a batch of run reports.

    Exists because the alternative is a caller logging "reported N" from
    the length of the list it handed over. That is what reported green for
    a day while nothing landed; a return value that counts what was
    delivered is the fix, and it costs one dataclass.
    """

    sent: int = 0
    suppressed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing failed to deliver."""
        return self.failed == 0


DEFAULT_DIMENSION = "pipeline_consistency"
"""Default dimension for self-reported findings.

Cogs can override via the ``dimension`` keyword if they want to report
on a different axis (e.g. ``data_quality``, ``freshness``).
"""


class Finding(TypedDict, total=False):
    """One row passed to :func:`post_findings`.

    Required keys:

    - ``severity`` — one of ``"SUCCESS"``, ``"WARN"``, ``"ERROR"``.
    - ``finding`` — the human-readable finding text.

    Optional keys:

    - ``dimension`` — defaults to :data:`DEFAULT_DIMENSION` when omitted.
    - ``suggestion`` — short remediation hint surfaced in the Pipeline
      Health UI alongside the finding.
    """

    severity: Severity
    finding: str
    dimension: str
    suggestion: str | None


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


@cache
def _resolve_processor_version(repo: str) -> str | None:
    """Return the installed distribution version for ``repo``, else ``None``.

    Pipeline Health findings benefit from carrying the version of the cog
    that emitted them — operators triaging a recurring WARN want to know
    which build introduced it. We resolve this once per ``repo`` via
    :func:`importlib.metadata.version` and cache the result for the
    lifetime of the process (cog distribution versions don't change
    in-flight).

    Returns ``None`` (rather than a marker like ``"0.0.0+local"``) when
    the distribution isn't installed under that name. Pipeline Health
    previously displayed ``(processor=0.0.0+local)`` on every voicenotes
    heartbeat because a per-cog helper used the pre-merge distribution
    name after the ``voicenotes-cog`` → ``transcription-cog`` merge
    (ADR-004). Centralising the resolution here, and treating "not
    installed" as "no suffix" rather than "stamp a marker", prevents
    that class of noise across every cog at once.

    The ``repo`` argument is the same string passed to
    :func:`post_findings` and :func:`post_run_finding` and must match the
    cog's ``pyproject.toml`` ``[project] name``. Mismatches (typos,
    pre-merge names) silently fall through to ``None`` — verify by
    looking at a real production finding after a release.
    """
    try:
        return version(repo)
    except PackageNotFoundError:
        # Editable dev checkouts, ad-hoc invocations, or a typoed repo
        # name. Better to emit no suffix than a misleading marker.
        return None
    except Exception:
        # importlib.metadata can raise other errors on broken metadata;
        # never let version lookup interfere with reporting itself.
        return None


def _stamp_processor_version(text: str, repo: str) -> str:
    """Append ``(processor=X.Y.Z)`` to ``text`` when the version resolves.

    No-op when :func:`_resolve_processor_version` returns ``None`` so
    Pipeline Health rows stay clean for editable dev checkouts. Also
    no-op when ``text`` already contains a ``(processor=…)`` fragment so
    callers that pre-stamp (or legacy emissions that pre-date this
    library) don't end up double-stamped.
    """
    if "(processor=" in text:
        return text
    resolved = _resolve_processor_version(repo)
    if not resolved:
        return text
    return f"{text} (processor={resolved})"


def _capture(exc: BaseException) -> None:
    """Report an exception to Sentry, if the host process has Sentry.

    Two conditions, both silent when unmet: ``sentry_sdk`` may not be
    installed — it is not a dependency of this library, and the mp3 and
    Google helpers should not start paying for one — and even when it is,
    ``capture_exception`` does nothing unless the process called ``init()``.
    Both no-op cleanly, so this is safe to call from anywhere, and reports
    only from cogs that actually wired layer three.
    """
    try:
        import sentry_sdk  # local import: optional dependency
    except ImportError:
        return
    with contextlib.suppress(Exception):
        sentry_sdk.capture_exception(exc)


# ---------------------------------------------------------------------------
# Sink 1 — graded findings, to POST /v1/evaluations
# ---------------------------------------------------------------------------


def _post_evaluation(payload: dict[str, Any]) -> bool:
    """POST one finding to ``/v1/evaluations``. Never raises.

    Returns whether the API accepted it. The boolean is the whole point:
    the previous version returned None, so a caller could not distinguish
    "delivered" from "swallowed", and for a day nothing did.
    """
    logger = get_prefect_logger()
    try:
        from mini_app_polis.api import KaianoApiClient  # local import
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: Kaiano API client not available; finding not posted"
            )
        _capture(exc)
        return False

    try:
        client = KaianoApiClient.from_env(machine_name=payload.get("repo"))
        client.post("/v1/evaluations", payload)
        return True
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.exception("pipeline_status: failed to POST finding (best-effort)")
        _capture(exc)
        return False


# ---------------------------------------------------------------------------
# Sink 2 — run status, to POST /v1/notify
# ---------------------------------------------------------------------------


def _build_message(
    *,
    repo: str,
    flow_name: str,
    run_id: str,
    severity: Severity,
    text: str,
    source: str,
    dimension: str,
    suggestion: str | None,
) -> dict[str, Any]:
    """Render one run report as a Discord message body.

    Everything a person needs to decide whether to go look, and nothing
    else. The run id is in the footer rather than the body because it is
    what you copy into Prefect once you have decided to.
    """
    embed: dict[str, Any] = {
        "title": f"{repo} · {flow_name}"[:256],
        "color": _SEVERITY_COLORS.get(severity, _DEFAULT_COLOR),
        "footer": {
            "text": f"{severity} · {source} · {dimension} · run {run_id}"[:2048]
        },
    }
    if text:
        embed["description"] = text[:4096]
    if suggestion:
        embed["fields"] = [
            {"name": "Suggestion", "value": suggestion[:1024], "inline": False}
        ]
    return {"embeds": [embed], "username": repo[:80]}


def _deliver(message: dict[str, Any], *, repo: str, logger: Any) -> bool:
    """POST one run report to ``/v1/notify``. Never raises.

    ``machine_name=repo`` is what makes the API's audit trail name which cog
    sent this. The cog's name, its distribution name and its machine name are
    the same string by convention, so there is nothing to keep in step — and
    a cog whose key variable is missing fails here loudly rather than
    authenticating as something else.
    """
    try:
        from mini_app_polis.api import KaianoApiClient  # local import
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: Kaiano API client not available; "
                "run report not sent (repo=%s)",
                repo,
            )
        _capture(exc)
        return False

    try:
        client = KaianoApiClient.from_env(machine_name=repo)
        client.notify(embeds=message["embeds"], username=message.get("username"))
        return True
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: run report not delivered (repo=%s)", repo
            )
        _capture(exc)
        return False


def post_findings(
    *,
    repo: str,
    flow_name: str,
    findings: Iterable[Finding],
    source: str = "flow_inline",
    production_only: bool = True,
) -> DeliveryReport:
    """Post one or more graded findings to ``/v1/evaluations``.

    This is the **findings** path and it still writes rows. Anything an
    evaluator judged against a revision of the standards catalog belongs
    here, because a finding is a record: it is compared, counted and burned
    down over time, and a notification cannot be any of those.

    What left this path is run status — "the flow finished", "the flow
    crashed" — which was never graded against anything and now goes to
    :func:`post_run_finding`. Keeping both here is what made the API null
    out ``standards_version`` on some rows to stop Pipeline Health claiming
    they had been evaluated.

    Each row is POSTed independently — one failed POST does not drop the
    others.

    Parameters
    ----------
    repo:
        Name of the repo the finding is about. Also the machine name used
        to authenticate, so the audit trail names the caller.
    flow_name:
        Name of the flow as it appears in Prefect.
    findings:
        Iterable of :class:`Finding` dicts. Each must have ``severity``
        and ``finding``; may carry ``dimension`` and ``suggestion``.
    source:
        Applied to **all** rows in this batch.
    production_only:
        When False, this call is a no-op regardless of env vars.

    Returns
    -------
    DeliveryReport
        What actually landed. A flow that logs "posted N" from the length
        of the list it handed over is the instrument that showed green
        while 162 findings went nowhere; log this instead.
    """
    logger = get_prefect_logger()
    rows = list(findings)

    if not _should_post(production_only):
        logger.debug(
            "pipeline_status: batch suppressed "
            "(repo=%s flow=%s rows=%d production_only=%s)",
            repo,
            flow_name,
            len(rows),
            production_only,
        )
        return DeliveryReport(suppressed=len(rows))

    if not rows:
        logger.debug(
            "pipeline_status: empty findings batch (repo=%s flow=%s)",
            repo,
            flow_name,
        )
        return DeliveryReport()

    run_id = get_run_id()
    sent = failed = skipped = 0

    for row in rows:
        severity: Severity = row.get("severity", "WARN")
        finding_text = (row.get("finding") or "").strip()
        if not finding_text:
            logger.warning(
                "pipeline_status: skipping row with empty finding text "
                "(repo=%s flow=%s severity=%s)",
                repo,
                flow_name,
                severity,
            )
            skipped += 1
            continue

        # Stamp the cog's installed distribution version onto the finding
        # text so Pipeline Health shows which build emitted a row. Done at
        # the library funnel rather than in each cog's adapter so every cog
        # gets it uniformly, and so a typoed cog-side helper cannot stamp a
        # marker like "0.0.0+local" instead of a real version (the original
        # voicenotes regression).
        finding_text = _stamp_processor_version(finding_text, repo)

        payload: dict[str, Any] = {
            "run_id": run_id,
            "repo": repo,
            "flow_name": flow_name,
            "dimension": row.get("dimension") or DEFAULT_DIMENSION,
            "severity": severity,
            "finding": finding_text,
            "source": source,
        }
        suggestion = row.get("suggestion")
        if suggestion is not None:
            payload["suggestion"] = suggestion

        try:
            if _post_evaluation(payload):
                sent += 1
            else:
                failed += 1
        except Exception as exc:
            # Defense in depth: _post_evaluation handles its own exceptions,
            # but post_findings must never propagate to flow code. Per-row
            # isolation: one bad POST does not abort the rest of the batch.
            failed += 1
            _capture(exc)
            with contextlib.suppress(Exception):
                logger.exception(
                    "pipeline_status: row POST raised unexpectedly "
                    "(should be best-effort) repo=%s flow=%s",
                    repo,
                    flow_name,
                )

    result = DeliveryReport(sent=sent, failed=failed, skipped=skipped)
    if result.failed:
        with contextlib.suppress(Exception):
            logger.error(
                "pipeline_status: %d of %d findings failed to post (repo=%s flow=%s)",
                result.failed,
                len(rows),
                repo,
                flow_name,
            )
    return result


def post_run_finding(
    flow_name: str,
    severity: Severity,
    text: str | None = None,
    *,
    repo: str,
    dimension: str = DEFAULT_DIMENSION,
    suggestion: str | None = None,
    production_only: bool = True,
    source: str = "flow_inline",
    **extras: Any,
) -> DeliveryReport:
    """Report one run outcome to the notification channel.

    Run status, not a finding — nothing is persisted. It no longer routes
    through :func:`post_findings`: sharing that funnel is what put run
    telemetry in the evaluations table, and two sinks with two meanings
    should not share one path back.

    ``SUCCESS`` is logged and goes no further; see :data:`NOTIFY_SEVERITIES`.

    Parameters
    ----------
    flow_name:
        Name of the flow as it appears in Prefect.
    severity:
        One of ``"SUCCESS"``, ``"WARN"``, ``"ERROR"``, ``"CRITICAL"``.
    text:
        Human-readable outcome. If omitted and severity is SUCCESS, a
        default of ``"Run completed successfully."`` is used.
    repo:
        Name of the cog (e.g. ``"deejay-cog"``). Required. Also the machine
        name the API authenticates and attributes the message to.
    dimension:
        Carried into the message footer. Retained for call-site
        compatibility; it no longer selects anything, since run reports are
        not graded against the standards catalog.
    suggestion:
        Optional remediation hint, rendered as a field on the message.
    production_only:
        When False, this call is a no-op regardless of env vars.
    source:
        ``"flow_inline"`` for end-of-flow calls, ``"flow_hook"`` for Prefect
        on_failure/on_crashed hook calls. Free-form otherwise.
    **extras:
        Cog-specific counters or flags. Non-zero values are appended to the
        text as ``k=v`` pairs (sorted alphabetically).

    Returns
    -------
    DeliveryReport
        Best-effort delivery never raises, and never claims success it did
        not get.
    """
    logger = get_prefect_logger()

    if severity == "SUCCESS" and text is None:
        text = "Run completed successfully."

    text_final = _merge_extras_into_text(text or "", extras).strip()

    if not _should_post(production_only):
        logger.debug(
            "pipeline_status: run report suppressed "
            "(repo=%s flow=%s production_only=%s)",
            repo,
            flow_name,
            production_only,
        )
        return DeliveryReport(suppressed=1)

    if not text_final:
        logger.warning(
            "pipeline_status: skipping run report with empty text "
            "(repo=%s flow=%s severity=%s)",
            repo,
            flow_name,
            severity,
        )
        return DeliveryReport(skipped=1)

    run_id = get_run_id()
    text_final = _stamp_processor_version(text_final, repo)

    if severity not in NOTIFY_SEVERITIES:
        # The successful-run case, which is most of them. Logged here and
        # nowhere else: absence is what Healthchecks.io watches.
        logger.info(
            "pipeline_status: %s %s/%s run=%s — %s",
            severity,
            repo,
            flow_name,
            run_id,
            text_final,
        )
        return DeliveryReport(suppressed=1)

    message = _build_message(
        repo=repo,
        flow_name=flow_name,
        run_id=run_id,
        severity=severity,
        text=text_final,
        source=source,
        dimension=dimension,
        suggestion=suggestion,
    )

    try:
        delivered = _deliver(message, repo=repo, logger=logger)
    except Exception as exc:
        # Defense in depth: _deliver handles its own exceptions, but this
        # must never propagate into flow code or a Prefect hook.
        _capture(exc)
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: run report raised unexpectedly "
                "(should be best-effort) repo=%s flow=%s",
                repo,
                flow_name,
            )
        return DeliveryReport(failed=1)

    if not delivered:
        with contextlib.suppress(Exception):
            logger.error(
                "pipeline_status: run report not delivered (repo=%s flow=%s)",
                repo,
                flow_name,
            )
        return DeliveryReport(failed=1)

    return DeliveryReport(sent=1)


def make_failure_hook(
    flow_name: str,
    *,
    repo: str,
    production_only: bool = True,
    dimension: str = DEFAULT_DIMENSION,
) -> Callable[..., None]:
    """Return a Prefect ``on_failure`` / ``on_crashed`` hook.

    The returned hook reports severity ``WARN`` for the ``Failed`` state
    and ``ERROR`` for ``Crashed``. Both reach the notification channel —
    this hook is the fleet's crash ping. It always logs the failure
    locally; delivery is gated the same way as :func:`post_run_finding`.

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
    "NOTIFY_SEVERITIES",
    "DeliveryReport",
    "Finding",
    "Severity",
    "get_prefect_logger",
    "get_run_id",
    "make_failure_hook",
    "post_findings",
    "post_run_finding",
]
