"""Unit tests for :mod:`mini_app_polis.pipeline_status`.

Covers the three guarantees the module promises:

1. **Gating** — ``production_only=False`` and missing
   ``KAIANO_API_BASE_URL`` both short-circuit before any HTTP call.
2. **Severity preservation** — SUCCESS reaches the API unchanged
   (regression test for the old evaluator-cog downgrade-to-WARN bug).
3. **Best-effort** — exceptions from the underlying HTTP client are
   logged but never propagate to callers.

Also pins payload-shape behaviour (no ``standards_version``, dimension
overridable, extras appended as text suffix) and failure-hook severity
mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mini_app_polis.pipeline_status as ps

# ---------------------------------------------------------------------------
# get_run_id
# ---------------------------------------------------------------------------


def test_get_run_id_local_run_when_no_runtime_or_env(monkeypatch) -> None:
    monkeypatch.delenv("PREFECT_FLOW_RUN_ID", raising=False)
    with patch("prefect.runtime.flow_run.id", None):
        assert ps.get_run_id() == "local-run"


def test_get_run_id_prefers_prefect_env_when_no_runtime_id(monkeypatch) -> None:
    monkeypatch.setenv("PREFECT_FLOW_RUN_ID", "run-from-env")
    with patch("prefect.runtime.flow_run.id", None):
        assert ps.get_run_id() == "run-from-env"


def test_get_run_id_prefers_runtime_id_over_env(monkeypatch) -> None:
    monkeypatch.setenv("PREFECT_FLOW_RUN_ID", "env-id")
    with patch("prefect.runtime.flow_run.id", "runtime-id"):
        assert ps.get_run_id() == "runtime-id"


def test_get_run_id_ignores_github_run_id(monkeypatch) -> None:
    monkeypatch.delenv("PREFECT_FLOW_RUN_ID", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "gha-999")
    with patch("prefect.runtime.flow_run.id", None):
        assert ps.get_run_id() == "local-run"


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_production_only_false_never_calls_api(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "test-flow",
            "SUCCESS",
            repo="test-cog",
            production_only=False,
        )
    post.assert_not_called()


def test_production_only_true_noop_without_base_url(monkeypatch) -> None:
    monkeypatch.delenv("KAIANO_API_BASE_URL", raising=False)
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="test-cog", production_only=True)
    post.assert_not_called()


def test_anthropic_api_key_not_required(monkeypatch) -> None:
    """Self-reported findings don't touch the LLM; no Anthropic key needed."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="test-cog", production_only=True)
    post.assert_called_once()


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


def test_payload_has_required_fields_and_repo(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "my-flow",
            "WARN",
            text="Something",
            repo="my-cog",
            production_only=True,
        )
    payload = post.call_args.args[0]
    assert payload["repo"] == "my-cog"
    assert payload["flow_name"] == "my-flow"
    assert payload["dimension"] == "pipeline_consistency"
    assert payload["finding"] == "Something"
    assert payload["severity"] == "WARN"
    assert payload["source"] == "flow_inline"
    assert "run_id" in payload


def test_payload_omits_standards_version(monkeypatch) -> None:
    """Regression: self-reported findings must not carry standards_version."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("STANDARDS_VERSION", "6.0")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
    assert "standards_version" not in post.call_args.args[0]


def test_dimension_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f",
            "SUCCESS",
            repo="my-cog",
            dimension="freshness",
            production_only=True,
        )
    assert post.call_args.args[0]["dimension"] == "freshness"


def test_explicit_source_is_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f",
            "WARN",
            text="bad",
            repo="my-cog",
            production_only=True,
            source="flow_hook",
        )
    assert post.call_args.args[0]["source"] == "flow_hook"


# ---------------------------------------------------------------------------
# Severity preservation
# ---------------------------------------------------------------------------


def test_success_severity_preserved(monkeypatch) -> None:
    """Regression: SUCCESS must reach the API unchanged.

    Previously, deejay-cog routed self-reports through evaluator-cog,
    which silently downgraded anything not in {INFO, WARN, ERROR} to
    WARN. The new direct path must not do that.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
    payload = post.call_args.args[0]
    assert payload["severity"] == "SUCCESS"
    assert payload["finding"] == "Run completed successfully."


def test_success_default_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
    assert post.call_args.args[0]["finding"] == "Run completed successfully."


# ---------------------------------------------------------------------------
# Extras → text suffix
# ---------------------------------------------------------------------------


def test_nonzero_extras_appended_to_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f",
            "SUCCESS",
            repo="my-cog",
            production_only=True,
            ingest_attempted=1,
        )
    assert post.call_args.args[0]["finding"] == (
        "Run completed successfully. ingest_attempted=1"
    )


def test_zero_extras_omitted_from_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f",
            "SUCCESS",
            repo="my-cog",
            production_only=True,
            ingest_attempted=0,
        )
    assert post.call_args.args[0]["finding"] == "Run completed successfully."


def test_multiple_extras_sorted_alphabetically(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f",
            "WARN",
            text="Issues",
            repo="my-cog",
            production_only=True,
            zebra=3,
            alpha=1,
            mid=2,
        )
    assert post.call_args.args[0]["finding"] == "Issues alpha=1; mid=2; zebra=3"


# ---------------------------------------------------------------------------
# Best-effort
# ---------------------------------------------------------------------------


def test_swallows_api_exception(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", side_effect=RuntimeError("boom")):
        # Must not raise.
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)


# ---------------------------------------------------------------------------
# make_failure_hook
# ---------------------------------------------------------------------------


def test_failure_hook_crashed_posts_error(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=True)
    state = SimpleNamespace(name="Crashed", type="CRASHED")
    with patch.object(ps, "_post_evaluation") as post:
        hook(None, None, state)
    payload = post.call_args.args[0]
    assert payload["severity"] == "ERROR"
    assert payload["source"] == "flow_hook"
    assert payload["repo"] == "my-cog"


def test_failure_hook_failed_posts_warn(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=True)
    state = SimpleNamespace(name="Failed", type="FAILED")
    with patch.object(ps, "_post_evaluation") as post:
        hook(None, None, state)
    assert post.call_args.args[0]["severity"] == "WARN"


def test_failure_hook_production_only_false_no_post(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=False)
    state = SimpleNamespace(name="Failed", type="FAILED")
    with patch.object(ps, "_post_evaluation") as post:
        hook(None, None, state)
    post.assert_not_called()


def test_failure_hook_swallows_post_run_finding_exception(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=True)
    state = SimpleNamespace(name="Failed", type="FAILED")
    mock_log = MagicMock()
    with (
        patch.object(ps, "post_run_finding", side_effect=RuntimeError("x")),
        patch.object(ps, "get_prefect_logger", return_value=mock_log),
    ):
        hook(None, None, state)
    mock_log.exception.assert_called()


# ---------------------------------------------------------------------------
# post_findings (multi-row)
# ---------------------------------------------------------------------------


def test_post_findings_posts_each_row(monkeypatch) -> None:
    """Every row in the batch becomes its own /v1/evaluations POST."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    findings = [
        {"severity": "WARN", "finding": "first issue"},
        {"severity": "WARN", "finding": "second issue"},
        {"severity": "ERROR", "finding": "third issue"},
    ]
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=findings,
            production_only=True,
        )
    assert post.call_count == 3
    posted_findings = [c.args[0]["finding"] for c in post.call_args_list]
    assert posted_findings == ["first issue", "second issue", "third issue"]


def test_post_findings_shared_fields_applied_to_every_row(monkeypatch) -> None:
    """run_id, repo, flow_name, and source are constant across the batch."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("PREFECT_FLOW_RUN_ID", "abc-123")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "a"},
                {"severity": "ERROR", "finding": "b"},
            ],
            source="flow_hook",
            production_only=True,
        )
    for call in post.call_args_list:
        payload = call.args[0]
        assert payload["repo"] == "my-cog"
        assert payload["flow_name"] == "my-flow"
        assert payload["source"] == "flow_hook"
        assert payload["run_id"] == "abc-123"


def test_post_findings_per_row_dimension_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "a", "dimension": "data_quality"},
                {"severity": "ERROR", "finding": "b"},  # default dimension
            ],
            production_only=True,
        )
    assert post.call_args_list[0].args[0]["dimension"] == "data_quality"
    assert post.call_args_list[1].args[0]["dimension"] == "pipeline_consistency"


def test_post_findings_suggestion_passed_through(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {
                    "severity": "WARN",
                    "finding": "the bad thing",
                    "suggestion": "do the thing differently",
                }
            ],
            production_only=True,
        )
    payload = post.call_args.args[0]
    assert payload["suggestion"] == "do the thing differently"


def test_post_findings_suggestion_omitted_when_none(monkeypatch) -> None:
    """Rows without a suggestion don't ship a key=None — keep payload minimal."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[{"severity": "WARN", "finding": "a"}],
            production_only=True,
        )
    assert "suggestion" not in post.call_args.args[0]


def test_post_findings_per_row_error_isolation(monkeypatch) -> None:
    """One row's POST failure does not prevent later rows from being attempted."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    seen: list[dict] = []

    def fake_post(payload):
        seen.append(payload)
        if payload["finding"] == "boom":
            raise RuntimeError("API exploded")

    with patch.object(ps, "_post_evaluation", side_effect=fake_post):
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "first"},
                {"severity": "ERROR", "finding": "boom"},
                {"severity": "WARN", "finding": "third"},
            ],
            production_only=True,
        )
    assert [p["finding"] for p in seen] == ["first", "boom", "third"]


def test_post_findings_skips_empty_finding_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "   "},  # whitespace-only
                {"severity": "WARN", "finding": ""},  # truly empty
                {"severity": "WARN", "finding": "valid"},
            ],
            production_only=True,
        )
    assert post.call_count == 1
    assert post.call_args.args[0]["finding"] == "valid"


def test_post_findings_empty_batch_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[],
            production_only=True,
        )
    post.assert_not_called()


def test_post_findings_production_only_false_no_post(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[{"severity": "WARN", "finding": "a"}],
            production_only=False,
        )
    post.assert_not_called()


def test_post_findings_no_base_url_no_post(monkeypatch) -> None:
    monkeypatch.delenv("KAIANO_API_BASE_URL", raising=False)
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[{"severity": "WARN", "finding": "a"}],
            production_only=True,
        )
    post.assert_not_called()


# ---------------------------------------------------------------------------
# post_run_finding ↔ suggestion plumbing
# ---------------------------------------------------------------------------


def test_post_run_finding_forwards_suggestion(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "my-flow",
            "WARN",
            text="something off",
            repo="my-cog",
            suggestion="check the logs",
            production_only=True,
        )
    payload = post.call_args.args[0]
    assert payload["suggestion"] == "check the logs"


def test_post_run_finding_no_suggestion_means_field_absent(monkeypatch) -> None:
    """suggestion=None must not appear in the payload at all."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
    assert "suggestion" not in post.call_args.args[0]


# ---------------------------------------------------------------------------
# Processor version stamping
# ---------------------------------------------------------------------------
#
# The library appends ``(processor=X.Y.Z)`` to every finding text so the
# Pipeline Health UI shows operators which build emitted a row. Stamping
# happens at the post_findings funnel so every cog routing through the
# library — voicenotes, deejay-cog flows, transcription-cog WCS flow,
# anything new — gets it uniformly. The previous per-cog approach
# (transcription-cog had its own _processor_version helper) stamped
# ``(processor=0.0.0+local)`` in production because it queried the
# pre-merge distribution name ``"voicenotes-cog"``; centralising the
# resolution and treating "not installed" as "no suffix" prevents that
# class of regression.
#
# ``_resolve_processor_version`` is lru_cached on ``repo``, so every
# test in this block clears the cache to keep monkeypatches honest.


def _reset_version_cache() -> None:
    ps._resolve_processor_version.cache_clear()


def test_processor_version_appended_when_resolvable(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f", "SUCCESS", text="Run completed.", repo="my-cog", production_only=True
        )
    assert post.call_args.args[0]["finding"] == "Run completed. (processor=1.2.3)"


def test_processor_version_omitted_when_not_installed(monkeypatch) -> None:
    """No suffix when importlib.metadata can't find the distribution."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()

    def _missing(name: str) -> str:
        raise ps.PackageNotFoundError(name)

    monkeypatch.setattr(ps, "version", _missing)
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f", "SUCCESS", text="Run completed.", repo="my-cog", production_only=True
        )
    assert post.call_args.args[0]["finding"] == "Run completed."


def test_processor_version_swallows_unexpected_errors(monkeypatch) -> None:
    """A broken distribution must not break reporting."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()

    def _boom(name: str) -> str:
        raise RuntimeError("broken metadata")

    monkeypatch.setattr(ps, "version", _boom)
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "f", "SUCCESS", text="Run completed.", repo="my-cog", production_only=True
        )
    assert post.call_args.args[0]["finding"] == "Run completed."


def test_processor_version_not_double_stamped(monkeypatch) -> None:
    """Caller-supplied ``(processor=...)`` text is left alone."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[
                {"severity": "WARN", "finding": "Already labeled (processor=9.9.9)"}
            ],
            production_only=True,
        )
    assert post.call_args.args[0]["finding"] == "Already labeled (processor=9.9.9)"


def test_processor_version_applied_to_every_row(monkeypatch) -> None:
    """Multi-row post_findings stamps every row, not just the first."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[
                {"severity": "WARN", "finding": "first"},
                {"severity": "ERROR", "finding": "second"},
            ],
            production_only=True,
        )
    findings = [c.args[0]["finding"] for c in post.call_args_list]
    assert findings == ["first (processor=1.2.3)", "second (processor=1.2.3)"]


def test_processor_version_cached_per_repo(monkeypatch) -> None:
    """importlib.metadata.version is consulted once per (repo) lookup."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    calls: list[str] = []

    def _counting(name: str) -> str:
        calls.append(name)
        return "1.2.3"

    monkeypatch.setattr(ps, "version", _counting)
    with patch.object(ps, "_post_evaluation"):
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
        ps.post_run_finding("f", "SUCCESS", repo="my-cog", production_only=True)
        ps.post_run_finding("f", "SUCCESS", repo="other-cog", production_only=True)
    # One call per distinct repo, regardless of post count.
    assert sorted(calls) == ["my-cog", "other-cog"]


def test_processor_version_failure_hook_stamps(monkeypatch) -> None:
    """Failure hooks emit through the library too — they get stamped."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=True)
    state = SimpleNamespace(name="Failed", type="FAILED")
    with patch.object(ps, "_post_evaluation") as post:
        hook(None, None, state)
    assert "(processor=1.2.3)" in post.call_args.args[0]["finding"]


# ---------------------------------------------------------------------------
# CRITICAL severity
# ---------------------------------------------------------------------------
#
# CRITICAL was added to the Severity Literal for the pre-flow process
# lifecycle only — serve_resilience.serve_with_retry emits it with
# source="startup" when a cog cannot register its deployments and is
# exiting. Before that, the enum was {SUCCESS, WARN, ERROR} and the
# module docstring argued CRITICAL was unreachable from a self-report
# ("a cog that is critically broken can't reach this code path").
#
# These tests pin the two properties that matter: CRITICAL reaches the
# API verbatim (no downgrade, the same class of bug as the old
# evaluator-cog SUCCESS→WARN downgrade), and it remains scoped to
# startup — the flow-run paths still cap out at ERROR.


def test_critical_severity_preserved(monkeypatch) -> None:
    """Regression: CRITICAL must reach the API unchanged, not downgraded."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_run_finding(
            "startup",
            "CRITICAL",
            text="Registration failed",
            repo="my-cog",
            production_only=True,
            source="startup",
        )
    payload = post.call_args.args[0]
    assert payload["severity"] == "CRITICAL"
    assert payload["source"] == "startup"


def test_critical_in_severity_literal() -> None:
    """The Literal itself is the contract consumers type-check against."""
    from typing import get_args

    assert set(get_args(ps.Severity)) == {"SUCCESS", "WARN", "ERROR", "CRITICAL"}


def test_critical_available_via_post_findings_batch(monkeypatch) -> None:
    """CRITICAL survives the multi-row funnel as well as the sugar wrapper."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    with patch.object(ps, "_post_evaluation") as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="startup",
            findings=[{"severity": "CRITICAL", "finding": "process is exiting"}],
            source="startup",
            production_only=True,
        )
    assert post.call_args.args[0]["severity"] == "CRITICAL"


def test_failure_hook_never_emits_critical(monkeypatch) -> None:
    """Scope guard: flow-run outcomes cap at ERROR, however bad the state.

    CRITICAL means "no flow ran at all". If make_failure_hook ever starts
    emitting it, the one signal that distinguishes a dead process from a
    crashed run is diluted.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=True)
    for state in (
        SimpleNamespace(name="Crashed", type="CRASHED"),
        SimpleNamespace(name="Failed", type="FAILED"),
        SimpleNamespace(name="Weird", type="SOMETHING_ELSE"),
    ):
        with patch.object(ps, "_post_evaluation") as post:
            hook(None, None, state)
        assert post.call_args.args[0]["severity"] in {"WARN", "ERROR"}
