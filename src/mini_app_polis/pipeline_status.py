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

Posts are gated by ``production_only=True`` (the default) **and** a base
URL resolving for this environment — ``KAIANO_API_BASE_URL`` in
production, ``KAIANO_API_BASE_URL_DEV`` everywhere else, resolved by
``mini_app_polis.environment``. A run with no base URL for its
environment logs a WARNING and delivers nothing.

``production_only`` is a misnomer kept for compatibility: it has never
consulted the environment, and never gated on it. It means "this caller
delivers at all" — deejay-cog's manual flows pass ``production_only=False``
so their reports stay local. Renaming it is a 6.0 change; every consumer
pins ``>=5,<6``. Development *does* deliver, to the development API.

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

A ``SUCCESS`` run report is logged rather than sent, unless the caller
marks it ``notable=True`` — the run was triggered by something rather
than being an idle scheduled tick. See :data:`NOTIFY_SEVERITIES` and
:func:`post_run_finding`. Findings are never suppressed by severity: a
SUCCESS finding is a graded result and belongs in the table.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, TypedDict

from mini_app_polis import logger as logger_mod
from mini_app_polis.environment import (
    Environment,
    api_base_url,
    current_environment,
    env_var_name,
)

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

#: Severities that always reach the notification channel, whatever else is
#: true of the run. SUCCESS is not among them: a fleet announcing "ran
#: fine" on every schedule is the noise problem this path exists to avoid,
#: and Healthchecks.io already answers "did it run" by firing on absence.
#:
#: A SUCCESS run still sends when the caller passes ``notable=True`` — see
#: :func:`post_run_finding`. The rule the two encode together: an idle tick
#: is silent, a triggered run is not, whether or not it produced anything.
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
    """Return True iff a self-reported finding should actually be POSTed.

    The base URL is resolved the same way :class:`KaianoApiClient` resolves
    it, so the gate and the destination answer one question rather than
    two. Reading the unsuffixed variable here while the client read the
    suffixed one meant a development process could pass this gate and post
    to production, or fail it while a perfectly good dev URL was set.

    Two reasons to say no, and they are not the same kind of thing. A
    caller that passed ``production_only=False`` chose this, and says so
    every run; nothing is wrong. A missing base URL is a misconfiguration,
    and the environment likely to have one is the environment nobody is
    watching — so it is logged at WARNING rather than left to the
    caller's DeliveryReport, which reads as fine either way.
    """
    if not production_only:
        return False
    if api_base_url():
        return True
    with contextlib.suppress(Exception):
        get_prefect_logger().warning(
            "pipeline_status: no API base URL resolved for environment=%s — "
            "nothing will be delivered. Set %s.",
            current_environment().value,
            env_var_name("KAIANO_API_BASE_URL"),
        )
    return False


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


def _environment_prefix() -> str:
    """``"[DEVELOPMENT] "`` outside production, empty string inside it.

    Only non-production is marked. A tag on every production message
    would be decoration on almost all channel traffic and would train
    the eye to skip the prefix entirely — the same reason the watcher's
    baseline warning is suppressed for a folder that is never empty.
    What is worth seeing is the message that did not come from
    production.
    """
    env = current_environment()
    if env is Environment.PRODUCTION:
        return ""
    return f"[{env.value.upper()}] "


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
        # Prefix, not a footer field: the footer is where the metadata
        # lives and is the part of an embed people skim past. The one
        # thing that must not be missed is that this did not come from
        # production.
        "title": f"{_environment_prefix()}{repo} · {flow_name}"[:256],
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
    notable: bool = False,
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
    notable:
        Send this even at ``SUCCESS``. Set it when the run was *triggered*
        — a file arrived, a webhook fired, a condition was met — rather
        than being a bare scheduled tick.

        Deliberately not inferred from the counters. A triggered run that
        then processed nothing has all-zero counters and is precisely the
        case worth hearing about: something said there was work, and none
        happened. Inferring from output would silence exactly that run.
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

    if severity not in NOTIFY_SEVERITIES and not notable:
        # An idle scheduled tick: nothing was waiting, nothing was done.
        # Logged here and nowhere else — absence is what Healthchecks.io
        # watches, so a quiet poller needs no message to prove it ran.
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


# ---------------------------------------------------------------------------
# Run reports
# ---------------------------------------------------------------------------

#: How many offending items are named per reason before the rest become a
#: count. Enough to recognise the pattern from the channel; not enough to
#: turn a bad batch into a wall of text nobody reads to the end of.
MAX_EXAMPLES = 3

#: Same idea for outcomes, and a little more generous. An outcome is what
#: the reader came for — a batch that created six things should name all
#: six rather than three and a number.
MAX_OUTCOMES = 5

CREATED = "created"
UPDATED = "updated"
REMOVED = "removed"

#: Render order and mark per operation. Deliberately the same three marks
#: the API's request middleware already puts in the activity channel, so
#: a task this cog created in Asana and a row the API wrote read the same
#: way to someone scrolling one server.
_OUTCOME_MARKS: tuple[tuple[str, str], ...] = (
    (CREATED, "+"),
    (UPDATED, "~"),
    (REMOVED, "-"),
)


@dataclass(frozen=True)
class Outcome:
    """One thing a run made exist, change, or stop existing.

    The gap this closes: every other verb on :class:`RunReport` records a
    problem or a count, so a clean run could say ``processed=1`` and
    nothing more. A voice note that became an Asana task reported exactly
    that — one file seen — while the task id it had just created was
    computed, returned, and dropped one frame before anything reached
    Discord.

    ``kind`` is the noun as the reader thinks of it ("asana task", "wiki
    page", "dj set"), not a table or class name. ``item`` names the
    specific one; ``link`` makes it clickable where there is somewhere to
    go. An outcome with no ``item`` still counts — some runs know they
    wrote four things without having a name for each.
    """

    op: str
    kind: str
    item: str | None = None
    link: str | None = None

    def label(self) -> str:
        """The item as it appears in the message, linked when possible."""
        if not self.item:
            return ""
        if self.link:
            return f"[{self.item}]({self.link})"
        return self.item


def _now() -> float:
    """The monotonic clock, behind one name so a test can move it.

    Called through rather than bound as a ``default_factory``: binding it
    captures the function object at class-definition time, and a patched
    clock would then move the end of a run without moving its start.
    """
    return time.monotonic()


def format_duration(seconds: float) -> str:
    """Render a run duration the way a person reads one.

    Sub-second runs keep two decimals so "it did nothing and took no time"
    is distinguishable from "it did nothing slowly"; past a minute the
    decimals stop earning their place.
    """
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass
class RunReport:
    """What a run did, accumulated as it happens and sent once at the end.

    Nothing here forces a call site to report a problem it swallowed;
    there is no mechanism that could, and a rule pretending otherwise
    would measure paperwork. What this removes is every other reason not
    to. No severity to decide, no text to compose, no counters threaded
    through return values, and — used as a context manager — no send call
    to remember. One verb, at the point where the problem is known.

    Four verbs, mapped onto the severity vocabulary this module already
    documents rather than a new one:

    ``ok(n)``
        ``n`` items were handled cleanly.
    ``note(reason, item)``
        Something was skipped for an ordinary reason. Counted, and never
        raises severity: "already processed", "not a spreadsheet", "the
        archive folder". These exist so the count in the message adds up,
        not because anyone needs to act on them.
    ``issue(reason, item, detail=...)``
        Something a human should look at. Any issue makes the run WARN.

    ``created(kind, item)`` / ``updated(...)`` / ``removed(...)``
        Something now exists, differs, or is gone outside this process.
        Recorded as an :class:`Outcome`, rendered above the problems, and
        never affecting severity — creating a thing is what a healthy run
        does. Recording one also makes the run ``notable``: a run that
        changed the world is worth hearing about as a matter of fact,
        rather than of per-flow configuration.

    There is deliberately no verb for ERROR. ERROR means a flow died, and
    a flow that died is reported by :func:`make_failure_hook` with the
    Prefect state that killed it — a state this object does not have and
    should not guess at.

    The report times itself from construction to :meth:`send`, so the
    duration on the headline is the run's and not a stopwatch every flow
    has to remember to carry. A caller that already measured its own work
    — or that builds the report after the fact, from a crash hook —
    passes ``duration_sec``, and that wins.

    ``count(key, value)`` carries a domain counter that says nothing about
    severity (tracks read, bytes written). It becomes an ordinary extra on
    the message, subject to the same non-zero filtering as before.
    """

    flow_name: str
    repo: str
    production_only: bool = True
    #: Explicit duration in seconds. ``None`` means "measure it yourself",
    #: which is right for every report opened at the start of the work.
    duration_sec: float | None = None

    processed: int = 0
    notes: Counter = field(default_factory=Counter)
    issues: Counter = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)

    _sent: bool = field(default=False, repr=False)
    _started_at: float = field(default_factory=lambda: _now(), repr=False)
    _ended_at: float | None = field(default=None, repr=False)

    # -- recording ---------------------------------------------------------

    def ok(self, n: int = 1) -> None:
        """Record ``n`` items handled cleanly."""
        self.processed += n

    def note(self, reason: str, item: str | None = None) -> None:
        """Record an ordinary skip. Counted; never raises severity."""
        self.notes[reason] += 1
        self._remember(reason, item)

    def issue(
        self, reason: str, item: str | None = None, *, detail: str | None = None
    ) -> None:
        """Record something a human should look at. Makes the run WARN."""
        self.issues[reason] += 1
        self._remember(reason, item if detail is None else f"{item or ''} ({detail})")

    def count(self, key: str, value: Any) -> None:
        """Carry a domain counter that has no bearing on severity."""
        self.counters[key] = value

    def created(
        self, kind: str, item: str | None = None, *, link: str | None = None
    ) -> None:
        """Record that this run brought something into existence."""
        self.outcomes.append(Outcome(CREATED, kind, item, link))

    def updated(
        self, kind: str, item: str | None = None, *, link: str | None = None
    ) -> None:
        """Record that this run changed something that already existed."""
        self.outcomes.append(Outcome(UPDATED, kind, item, link))

    def removed(
        self, kind: str, item: str | None = None, *, link: str | None = None
    ) -> None:
        """Record that this run deleted or retired something."""
        self.outcomes.append(Outcome(REMOVED, kind, item, link))

    def _remember(self, reason: str, item: str | None) -> None:
        """Keep the first few offenders for this reason, and no more."""
        if not item:
            return
        seen = self.examples.setdefault(reason, [])
        if len(seen) < MAX_EXAMPLES:
            seen.append(str(item).strip())

    # -- rendering ---------------------------------------------------------

    @property
    def severity(self) -> Severity:
        """WARN if anything was flagged, SUCCESS otherwise.

        Outcomes never enter into it. A run that created nine things and
        broke nothing is a SUCCESS; what the outcomes change is whether
        that SUCCESS is worth sending, not what it is called.
        """
        return "WARN" if self.issues else "SUCCESS"

    @property
    def duration(self) -> float:
        """Seconds this run took, measured unless the caller supplied it."""
        if self.duration_sec is not None:
            return max(0.0, float(self.duration_sec))
        end = _now() if self._ended_at is None else self._ended_at
        return max(0.0, end - self._started_at)

    def outcome_lines(self) -> list[str]:
        """One line per operation and kind, in a fixed order.

        Grouped rather than one line per thing: twelve tasks created is a
        fact about the run, and twelve consecutive identical-looking lines
        is how the problem underneath them ends up below the fold.
        """
        lines: list[str] = []
        for op, mark in _OUTCOME_MARKS:
            by_kind: dict[str, list[Outcome]] = {}
            for outcome in self.outcomes:
                if outcome.op == op:
                    by_kind.setdefault(outcome.kind, []).append(outcome)
            for kind in sorted(by_kind):
                rows = by_kind[kind]
                named = [r.label() for r in rows if r.item]
                shown = named[:MAX_OUTCOMES]
                if not shown:
                    lines.append(f"{mark} {kind} x{len(rows)}")
                    continue
                more = len(rows) - len(shown)
                suffix = f", +{more} more" if more > 0 else ""
                lines.append(f"{mark} {kind}: {', '.join(shown)}{suffix}")
        return lines

    def tally(self) -> str:
        """The one-line count, in a fixed order so two runs compare."""
        parts: list[str] = []
        if self.processed:
            parts.append(f"processed={self.processed}")
        for reason, n in sorted(self.issues.items()):
            parts.append(f"{reason}={n}")
        for reason, n in sorted(self.notes.items()):
            parts.append(f"{reason}={n}")
        return ", ".join(parts)

    def text(self) -> str:
        """The message body: the tally, what the run did, then what it flagged.

        Outcomes come before problems because a clean run has only
        outcomes, and that is the whole message on a good day.

        Among the problems, only flagged reasons name names. An ordinary
        skip is counted and left at that — naming every already-processed
        file is how the interesting line ends up below the fold.
        """
        tally = self.tally()
        took = f"Run complete in {format_duration(self.duration)}"
        body = f"{took} — {tally}." if tally else f"{took} — nothing to do."

        lines = [body]
        lines.extend(self.outcome_lines())
        for reason in sorted(self.issues):
            named = self.examples.get(reason) or []
            if not named:
                continue
            more = self.issues[reason] - len(named)
            suffix = f", +{more} more" if more > 0 else ""
            lines.append(f"{reason}: {', '.join(named)}{suffix}")
        return "\n".join(lines)

    # -- delivery ----------------------------------------------------------

    def send(
        self,
        *,
        notable: bool = False,
        source: str = "flow_inline",
        suggestion: str | None = None,
    ) -> DeliveryReport:
        """Post the accumulated run report. Safe to call twice; the second
        call does nothing, so an explicit ``send()`` inside a ``with`` block
        does not produce a second message.

        Any recorded outcome makes the run notable. A SUCCESS is otherwise
        logged and goes no further, which is right for an idle tick and is
        the reason a voice note could become an Asana task in silence. The
        caller keeps the flag for the other case — a triggered run that did
        nothing, which has no outcome to speak for it.
        """
        if self._sent:
            return DeliveryReport(suppressed=1)
        self._sent = True
        self._ended_at = _now()
        return post_run_finding(
            self.flow_name,
            self.severity,
            text=self.text(),
            repo=self.repo,
            production_only=self.production_only,
            source=source,
            suggestion=suggestion,
            notable=notable or bool(self.outcomes),
            **self.counters,
        )


@contextmanager
def run_report(
    flow_name: str,
    *,
    repo: str,
    production_only: bool = True,
    notable: bool = False,
    source: str = "flow_inline",
) -> Iterator[RunReport]:
    """Open a :class:`RunReport` that sends itself however the block ends.

    Forgetting to *record* a problem is still possible, and always will
    be. Forgetting to *send* is not, which is the half of the problem a
    common interface can actually take away.

    On an exception the accumulated report is sent with the exception
    recorded as an issue, and the exception is re-raised unchanged — so
    the flow still fails and ``make_failure_hook`` still fires. That is
    two messages about one bad run, and it is the same trade the Prefect
    webhook backstop already makes: the hook knows the Prefect state, this
    knows what the run had managed to do first, and neither is derivable
    from the other. They are told apart by ``source``.

    ``BaseException`` is caught rather than ``Exception`` so a run killed
    by SIGTERM mid-batch still says what it had processed. It is re-raised
    either way, and :func:`post_run_finding` is documented never to raise.
    """
    report = RunReport(flow_name=flow_name, repo=repo, production_only=production_only)
    try:
        yield report
    except BaseException as exc:
        report.issue("unhandled_exception", type(exc).__name__, detail=str(exc))
        report.send(notable=True, source=source)
        raise
    report.send(notable=notable, source=source)


__all__ = [
    "CREATED",
    "DEFAULT_DIMENSION",
    "MAX_EXAMPLES",
    "MAX_OUTCOMES",
    "NOTIFY_SEVERITIES",
    "REMOVED",
    "UPDATED",
    "DeliveryReport",
    "Finding",
    "Outcome",
    "RunReport",
    "Severity",
    "format_duration",
    "get_prefect_logger",
    "get_run_id",
    "make_failure_hook",
    "post_findings",
    "post_run_finding",
    "run_report",
]
