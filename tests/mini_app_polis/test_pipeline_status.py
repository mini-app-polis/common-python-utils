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
