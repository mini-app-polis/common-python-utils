"""Shared pipeline-status reporting for Kaiano cogs.

Each cog (deejay-cog, evaluator-cog, transcription-cog, …) runs as a
Lambda worker or a Railway one-shot and reports what happened. This
module centralises that wiring so no cog has to hand-roll the helpers or
payload shapes.

**Two sinks, because there are two different things being reported.**

:func:`post_findings` → ``POST /v1/evaluations``. Graded findings: what
an evaluator judged about a repo against a revision of the standards
catalog. These are records. They are compared, counted and burned down
over time, so they are written to a table and stay there.

:func:`post_run_finding` and :class:`RunReport` → ``POST
/v1/notify``. Run status: this flow finished, this flow crashed. Nothing
is persisted, and nothing grades "the flow crashed" against the standards
catalog, so run status carries no ``standards_version``.

The two do not share a funnel. A shared one would let run telemetry into
the evaluations table, and a single change to "the sink" would then move
both.

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
- :meth:`RunReport.send` — a run's issues and outcomes, accumulated as
  the work goes and reported as one message (:func:`run_report` sends it
  however the block ends).

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
delivers at all" — a caller passes ``production_only=False`` to keep its
reports local. Renaming it is a major-release change.

Run reports are posted from production only: the runs channel is
production's record, and a development run says what it did in its own
logs. Graded findings (:func:`post_findings`) still go to the development
API from development, since they are records, not messages.

The fleet does not run on Prefect. :func:`get_prefect_logger` and
:func:`get_run_id` import it lazily, and fall back when it is absent,
because callers use them; ``mini_app_polis`` does not declare Prefect as
a dependency.

Severity classification:

  SUCCESS  — run completed end-to-end; no issues a human needs to
             review. Includes "nothing to do" (empty input) and
             intentional skip paths.
  WARN     — run completed but produced results worth a human look
             (e.g. some items failed, some inputs malformed).
  ERROR    — the run itself crashed or terminally failed.
  CRITICAL — process-level failure with no run at all: the cog could
             not start and is exiting. Nothing in this library emits it
             any more. See the :data:`Severity` docstring for why it is
             deliberately narrow.

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
import warnings
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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
             flow run exists to report on. Pre-flow lifecycle only.

``INFO`` is intentionally absent from this enum. It belongs to the
LLM-evaluator and webhook paths, where a neutral observation ("flow
entered Running state", "config snapshot recorded") is a useful
signal. For a self-report at end-of-run there is no informational
outcome distinct from SUCCESS — a clean "nothing to do" run is still
a success. Collapsing INFO into SUCCESS keeps the cog's mental model
"did I do my job?" rather than "what category of event was this?".

**On CRITICAL.** CRITICAL is reserved for signals no single *flow
run* can authoritatively raise: a cog that is critically broken
typically can't reach this code path at all — it dies before reporting.
The one case a cog can report is a failure at startup, before any flow
run exists to report on. Nothing in this library emits CRITICAL; the
value exists so a consumer that has that need can.

CRITICAL is therefore **narrowly scoped**:

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

    **Every caller should prefer passing its own run id.** Both resolution
    steps above are Prefect's, and a cog that does not run under Prefect
    reaches the ``"local-run"`` fallback on every invocation — so its run
    reports all arrive under one id that identifies nothing and cannot be
    joined to the findings the same run filed. ``post_findings``,
    ``post_run_finding`` and :class:`RunReport` all take ``run_id`` for
    that reason; this function is the fallback for callers that have none,
    and becomes a dead path once the last cog is converted.
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

    Called when building the counter sections appended to the run report's text:
    only counters that actually have something to say are shown.
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


#: The section counters go under when their caller names none.
DEFAULT_COUNTER_SECTION = "Counts"

#: Indents an item under its section's label.
_INDENT = "  "


def counter_line(key: str, value: Any, label: str | None = None) -> str:
    """One counter in plain words: ``42 tasks checked``.

    ``label`` is what the counter is called (``"tasks checked"``); a label
    with ``{}`` in it places the value itself (``"${} spent"``). Without
    one, the counter's name is read as words (``tasks_checked`` ->
    ``tasks checked``), so no counter is ever shown as ``name=value``.
    """
    words = label or key.replace("_", " ")
    if "{}" in words:
        return words.replace("{}", str(value))
    return f"{value} {words}"


def counter_sections(
    extras: dict[str, Any],
    labels: dict[str, str] | None = None,
    sections: dict[str, str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Non-zero ``extras`` as labelled sections of plain-word lines.

    ``sections`` puts a counter under a section's label (``"Board"``);
    the rest go under :data:`DEFAULT_COUNTER_SECTION`. Sections keep the
    order their first counter came in, and counters within one theirs.
    """
    grouped: dict[str, list[str]] = {}
    for key, value in _nonzero_extras(extras).items():
        section = (sections or {}).get(key, DEFAULT_COUNTER_SECTION)
        grouped.setdefault(section, []).append(
            counter_line(key, value, (labels or {}).get(key))
        )
    return list(grouped.items())


def render_sections(sections: Iterable[tuple[str, list[str]]]) -> str:
    """Labelled sections, one item a line, a blank line between them."""
    blocks = [
        "\n".join([label, *(f"{_INDENT}{line}" for line in lines)])
        for label, lines in sections
        if lines
    ]
    return "\n\n".join(blocks)


def _merge_extras_into_text(
    text: str,
    extras: dict[str, Any],
    labels: dict[str, str] | None = None,
    sections: dict[str, str] | None = None,
) -> str:
    """``text``, then the non-zero ``extras`` as sections of plain words."""
    rendered = render_sections(counter_sections(extras, labels, sections))
    if not rendered:
        return text
    return f"{text}\n\n{rendered}" if text else rendered


def stamp_version(text: str, version: str | None) -> str:
    """Add ``(version=X.Y.Z)`` to the first line of ``text``: the build that
    sent it.

    ``version`` is the caller's own ``__version__``, from its
    ``_version.py``. Never installed package metadata: a Lambda deploy
    strips ``*.dist-info``, so a lookup there finds nothing in production,
    and a lookup by the wrong distribution name stamps the wrong build.

    No-op when ``version`` is empty, and when ``text`` already carries a
    stamp, so a caller that stamped its own text is not stamped twice.
    """
    if not version or "(version=" in text:
        return text
    # On the headline, so the stamp stays with what the run says about
    # itself rather than trailing the last counter of the last section.
    headline, sep, rest = text.partition("\n")
    return f"{headline} (version={version}){sep}{rest}"


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
    what you search the logs for once you have decided to.
    """
    del source, dimension  # carried by the caller; not shown on the message
    embed: dict[str, Any] = {
        # Prefix, not a footer field: the footer is where the metadata
        # lives and is the part of an embed people skim past. The one
        # thing that must not be missed is that this did not come from
        # production.
        "title": f"{_environment_prefix()}{repo} · {flow_name}"[:256],
        "color": _SEVERITY_COLORS.get(severity, _DEFAULT_COLOR),
        # Severity first: the API's cog-run detector reads it from here.
        "footer": {"text": f"{severity} · run {run_id[:8]}"[:2048]},
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
        from mini_app_polis.api.contract import NotifyRequest
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
        result = client.send_notification(
            NotifyRequest(embeds=message["embeds"], username=message.get("username"))
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.exception(
                "pipeline_status: run report not delivered (repo=%s)", repo
            )
        _capture(exc)
        return False

    # The API answers 200 with forwarded=false when it decided not to post
    # the message. That is not a delivery, and saying so is the point of
    # returning a bool at all.
    if not result.forwarded:
        with contextlib.suppress(Exception):
            logger.warning(
                "pipeline_status: run report not forwarded (repo=%s): %s",
                repo,
                result.reason,
            )
        return False
    return True


def post_findings(
    *,
    repo: str,
    flow_name: str,
    findings: Iterable[Finding],
    source: str = "flow_inline",
    production_only: bool = True,
    run_id: str | None = None,
    version: str | None = None,
) -> DeliveryReport:
    """Post one or more graded findings to ``/v1/evaluations``.

    .. deprecated::
        Use ``KaianoApiClient.create_evaluation``
        with a ``PipelineEvaluationCreate``. Removed in the next major
        release.

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
        Name of the flow, as it appears in the report.
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
    warnings.warn(
        "pipeline_status.post_findings is deprecated: use "
        "KaianoApiClient.create_evaluation. It will be removed in the next "
        "major release.",
        DeprecationWarning,
        stacklevel=2,
    )
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

    # The caller's own id wins. See get_run_id: its resolution order is
    # Prefect's, and a cog not under Prefect would otherwise file every row
    # under "local-run".
    run_id = run_id or get_run_id()
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

        finding_text = stamp_version(finding_text, version)

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
    run_id: str | None = None,
    version: str | None = None,
    counter_labels: dict[str, str] | None = None,
    counter_sections: dict[str, str] | None = None,
    **extras: Any,
) -> DeliveryReport:
    """Report one run outcome to the notification channel.

    Run status, not a finding — nothing is persisted. It does not route
    through :func:`post_findings`: sharing that funnel would put run
    telemetry in the evaluations table, and two sinks with two meanings
    should not share one path back.

    ``SUCCESS`` is logged and goes no further; see :data:`NOTIFY_SEVERITIES`.

    Parameters
    ----------
    flow_name:
        Name of the flow, as it appears in the report.
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
        compatibility; it selects nothing, since run reports are
        not graded against the standards catalog.
    suggestion:
        Optional remediation hint, rendered as a field on the message.
    production_only:
        When False, this call is a no-op regardless of env vars.
    source:
        ``"flow_inline"`` for end-of-flow calls. Free-form otherwise.
    notable:
        Send this even at ``SUCCESS``. Set it when the run was *triggered*
        — a file arrived, a webhook fired, a condition was met — rather
        than being a bare scheduled tick.

        Deliberately not inferred from the counters. A triggered run that
        then processed nothing has all-zero counters and is precisely the
        case worth hearing about: something said there was work, and none
        happened. Inferring from output would silence exactly that run.
    version:
        The caller's own ``__version__``, stamped on the text as
        ``(version=X.Y.Z)``; see :func:`stamp_version`.
    counter_labels:
        What each counter in ``extras`` is called, in words (see
        :func:`counter_line`). A counter with no label is read from its
        name.
    counter_sections:
        Which labelled section each counter goes under; the rest go under
        :data:`DEFAULT_COUNTER_SECTION`.
    **extras:
        Cog-specific counters or flags. Non-zero values are appended to the
        text in plain words, one a line, under their sections.

    Nothing is sent from outside production: the channel is production's
    record, and a development run says so in its logs alone.

    Returns
    -------
    DeliveryReport
        Best-effort delivery never raises, and never claims success it did
        not get.
    """
    logger = get_prefect_logger()

    if severity == "SUCCESS" and text is None:
        text = "Run completed successfully."

    text_final = _merge_extras_into_text(
        text or "", extras, counter_labels, counter_sections
    ).strip()

    if current_environment() is not Environment.PRODUCTION:
        logger.info(
            "pipeline_status: %s %s/%s not posted outside production — %s",
            severity,
            repo,
            flow_name,
            text_final,
        )
        return DeliveryReport(suppressed=1)

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

    run_id = run_id or get_run_id()
    text_final = stamp_version(text_final, version)

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
        # must never propagate into flow code.
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

#: The issue a run that ran out of time is counted under.
STOPPED_EARLY = "stopped early"
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

    Five verbs, mapped onto the severity vocabulary this module already
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
    ``notice(reason, item)``
        Something worth naming that is not a problem with this run: "task
        newly held". Named like an issue and makes the run ``notable``, but
        never raises severity, so a standing condition can be reported when
        it changes without turning every run that sees it into a WARN.

    ``created(kind, item)`` / ``updated(...)`` / ``removed(...)``
        Something now exists, differs, or is gone outside this process.
        Recorded as an :class:`Outcome`, rendered above the problems, and
        never affecting severity — creating a thing is what a healthy run
        does. Recording one also makes the run ``notable``: a run that
        changed the world is worth hearing about as a matter of fact,
        rather than of per-flow configuration.

    There is deliberately no verb for ERROR. ERROR means a flow died, and
    how it died is the caller's to say — this object does not know and
    should not guess at it.

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
    #: The run this report belongs to.
    #:
    #: ``None`` falls back to :func:`get_run_id`, whose resolution order is
    #: Prefect's — so a cog that does not set this
    #: sends every run report under ``"local-run"``, unattributable and
    #: unjoinable to the findings the same run filed. A caller that mints
    #: its own id should set this as soon as it has one; it may be assigned
    #: after construction, because the id is often minted from data the
    #: report was opened to collect.
    run_id: str | None = None
    #: The caller's own ``__version__``, stamped on the message; see
    #: :func:`stamp_version`.
    version: str | None = None

    processed: int = 0
    notes: Counter = field(default_factory=Counter)
    issues: Counter = field(default_factory=Counter)
    notices: Counter = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    counter_labels: dict[str, str] = field(default_factory=dict)
    counter_sections: dict[str, str] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    #: Set when the run stopped because its time ran out (see
    #: :func:`run_report`): the headline says so instead of "complete".
    stopped_early: str | None = None

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

    def notice(self, reason: str, item: str | None = None) -> None:
        """Record something worth naming. Makes the run notable; never WARN."""
        self.notices[reason] += 1
        self._remember(reason, item)

    def count(
        self,
        key: str,
        value: Any,
        *,
        label: str | None = None,
        section: str | None = None,
    ) -> None:
        """Carry a domain counter that has no bearing on severity.

        ``label`` is what it is called, in words (``"tasks checked"``, or
        ``"${} spent"`` to place the value); ``section`` is the labelled
        section it goes under. See :func:`counter_line`.
        """
        self.counters[key] = value
        if label:
            self.counter_labels[key] = label
        if section:
            self.counter_sections[key] = section

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

    def outcome_sections(self) -> list[tuple[str, list[str]]]:
        """One section per operation and kind, one item a line, in a fixed
        order.

        A section is headed by what happened (``task moved to Ready``) and
        lists what it happened to, so twelve tasks read as one fact with
        twelve names under it. Past :data:`MAX_OUTCOMES` the rest are a
        count; things recorded with no name are a count alone.
        """
        sections: list[tuple[str, list[str]]] = []
        for op, mark in _OUTCOME_MARKS:
            by_kind: dict[str, list[Outcome]] = {}
            for outcome in self.outcomes:
                if outcome.op == op:
                    by_kind.setdefault(outcome.kind, []).append(outcome)
            for kind in sorted(by_kind):
                rows = by_kind[kind]
                named = [r.label() for r in rows if r.item]
                shown = named[:MAX_OUTCOMES]
                lines = [f"{mark} {item}" for item in shown]
                more = len(rows) - len(shown)
                if more and shown:
                    lines.append(f"{mark} {more} more")
                elif more:
                    lines.append(f"{mark} {more}")
                sections.append((kind, lines))
        return sections

    def outcome_lines(self) -> list[str]:
        """The outcome sections as lines, for a caller composing its own."""
        rendered = render_sections(self.outcome_sections())
        return rendered.split("\n") if rendered else []

    def problem_sections(self) -> list[tuple[str, list[str]]]:
        """Notices, then issues, one section per reason naming what it hit,
        then ordinary skips as counts under one section."""
        sections: list[tuple[str, list[str]]] = []
        for counted in (self.notices, self.issues):
            for reason in sorted(counted):
                if reason == STOPPED_EARLY and self.stopped_early is not None:
                    continue  # the headline says it
                named = self.examples.get(reason) or []
                more = counted[reason] - len(named)
                lines = list(named)
                if more and named:
                    lines.append(f"{more} more")
                elif more:
                    lines.append(str(more))
                sections.append((reason, lines))
        if self.notes:
            sections.append(
                (
                    "Skipped",
                    [f"{n} {reason}" for reason, n in sorted(self.notes.items())],
                )
            )
        return sections

    def tally(self) -> str:
        """What the headline counts: handled, and problems, in words."""
        parts: list[str] = []
        if self.processed:
            parts.append(f"{self.processed} processed")
        problems = sum(self.issues.values())
        if problems:
            parts.append(f"{problems} to look at")
        return ", ".join(parts)

    def headline(self) -> str:
        """The first line: how the run ended, how long it took, and its
        counts."""
        took = format_duration(self.duration)
        if self.stopped_early is not None:
            return (
                f"Stopped early after {took}: out of time ({self.stopped_early}); "
                "the next run picks up the rest."
            )
        tally = self.tally()
        if tally:
            return f"Run complete in {took} — {tally}."
        if self.outcomes or self.notices or self.notes or self.counters:
            # Work recorded only below is still work: the sections say what
            # it was, so the headline must not call it nothing.
            return f"Run complete in {took}."
        return f"Run complete in {took} — nothing to do."

    def text(self) -> str:
        """The message body: the headline, what the run did, then what it
        flagged, as labelled sections with one item a line.

        Outcomes come before problems because a clean run has only
        outcomes, and that is the whole message on a good day. Counters are
        added below these when the report is sent (:func:`post_run_finding`).
        """
        sections = [*self.outcome_sections(), *self.problem_sections()]
        rendered = render_sections(sections)
        return f"{self.headline()}\n\n{rendered}" if rendered else self.headline()

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
            notable=notable or bool(self.outcomes) or bool(self.notices),
            run_id=self.run_id,
            version=self.version,
            counter_labels=self.counter_labels,
            counter_sections=self.counter_sections,
            **self.counters,
        )


def _out_of_time(exc: BaseException) -> bool:
    """Whether ``exc`` is the worker's planned stop before its timeout.

    Matched by name: importing the worker module here would make every
    report depend on it.
    """
    return type(exc).__name__ == "RunOutOfTime"


@contextmanager
def run_report(
    flow_name: str,
    *,
    repo: str,
    production_only: bool = True,
    notable: bool = False,
    source: str = "flow_inline",
    run_id: str | None = None,
    version: str | None = None,
) -> Iterator[RunReport]:
    """Open a :class:`RunReport` that sends itself however the block ends.

    Forgetting to *record* a problem is still possible, and always will
    be. Forgetting to *send* is not, which is the half of the problem a
    common interface can actually take away.

    On an exception the accumulated report is sent with the exception
    recorded as an issue, and the exception is re-raised unchanged — so
    the flow still fails and the caller's own failure handling still runs.

    ``BaseException`` is caught rather than ``Exception`` so a run killed
    by SIGTERM mid-batch still says what it had processed. It is re-raised
    either way, and :func:`post_run_finding` is documented never to raise.
    """
    report = RunReport(
        flow_name=flow_name,
        repo=repo,
        production_only=production_only,
        run_id=run_id,
        version=version,
    )
    try:
        yield report
    except BaseException as exc:
        if _out_of_time(exc):
            # A planned stop, not a crash: WARN, said once in the headline.
            report.stopped_early = str(exc) or "the function's timeout was near"
            report.issues[STOPPED_EARLY] += 1
        else:
            report.issue("unhandled_exception", type(exc).__name__, detail=str(exc))
        report.send(notable=True, source=source)
        raise
    report.send(notable=notable, source=source)


__all__ = [
    "CREATED",
    "DEFAULT_COUNTER_SECTION",
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
    "counter_line",
    "counter_sections",
    "format_duration",
    "get_prefect_logger",
    "get_run_id",
    "post_findings",
    "post_run_finding",
    "render_sections",
    "run_report",
    "stamp_version",
]
