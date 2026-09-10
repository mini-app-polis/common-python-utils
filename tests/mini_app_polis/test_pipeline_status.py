"""Unit tests for :mod:`mini_app_polis.pipeline_status`.

Covers the guarantees the module promises:

1. **Gating** — ``production_only=False`` and missing
   ``KAIANO_API_BASE_URL`` both short-circuit before any HTTP call.
2. **Severity handling** — SUCCESS is logged and never notified; WARN,
   ERROR and CRITICAL reach the channel verbatim (regression test for
   the old evaluator-cog downgrade-to-WARN bug).
3. **Best-effort** — exceptions from the underlying client are logged
   and reported, never propagated to callers.
4. **Truthful results** — a :class:`DeliveryReport` counts what was
   actually delivered. Best-effort must not mean claiming success.

Also pins message shape (severity/source/dimension in the footer,
suggestion as a field, extras appended as a text suffix) and
failure-hook severity mapping.

The seam is ``_deliver``, which is the only thing that touches the
network. ``_build_message`` is spied on with ``wraps`` so assertions can
name a semantic field rather than grep a rendered string, while the real
renderer still runs on every call.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import mini_app_polis.pipeline_status as ps


@contextmanager
def captured(*, deliver_returns: bool = True, deliver_raises: Exception | None = None):
    """Patch the network seam and spy on message construction."""
    kwargs = (
        {"side_effect": deliver_raises}
        if deliver_raises is not None
        else {"return_value": deliver_returns}
    )
    with (
        patch.object(ps, "_deliver", **kwargs) as deliver,
        patch.object(ps, "_build_message", wraps=ps._build_message) as build,
    ):
        yield SimpleNamespace(deliver=deliver, build=build)


def _built(cap, index: int = 0) -> dict:
    """Keyword arguments the module passed to the renderer for one row."""
    return cap.build.call_args_list[index].kwargs


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
    with captured() as cap:
        result = ps.post_run_finding(
            "test-flow", "WARN", text="x", repo="test-cog", production_only=False
        )
    cap.deliver.assert_not_called()
    assert result.suppressed == 1
    assert result.sent == 0


def test_production_only_true_noop_without_base_url(monkeypatch) -> None:
    monkeypatch.delenv("KAIANO_API_BASE_URL", raising=False)
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="x", repo="test-cog")
    cap.deliver.assert_not_called()


def test_anthropic_api_key_not_required(monkeypatch) -> None:
    """Run reports don't touch the LLM; no Anthropic key needed."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="x", repo="test-cog")
    cap.deliver.assert_called_once()


# ---------------------------------------------------------------------------
# Message shape
# ---------------------------------------------------------------------------


def test_message_carries_required_fields_and_repo(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding("my-flow", "WARN", text="Something", repo="my-cog")
    built = _built(cap)
    assert built["repo"] == "my-cog"
    assert built["flow_name"] == "my-flow"
    assert built["dimension"] == "pipeline_consistency"
    assert built["text"] == "Something"
    assert built["severity"] == "WARN"
    assert built["source"] == "flow_inline"
    assert built["run_id"]


def test_rendered_embed_shape(monkeypatch) -> None:
    """The rendered message is what a person actually sees."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("PREFECT_FLOW_RUN_ID", "run-77")
    with captured() as cap:
        ps.post_run_finding(
            "my-flow",
            "ERROR",
            text="it broke",
            repo="my-cog",
            suggestion="look at the logs",
        )
    message = cap.deliver.call_args.args[0]
    embed = message["embeds"][0]
    assert message["username"] == "my-cog"
    assert embed["title"] == "my-cog · my-flow"
    assert embed["description"] == "it broke"
    assert embed["color"] == ps._SEVERITY_COLORS["ERROR"]
    assert embed["fields"][0]["value"] == "look at the logs"
    footer = embed["footer"]["text"]
    assert "ERROR" in footer
    assert "flow_inline" in footer
    assert "run-77" in footer


def test_build_message_marks_non_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    message = ps._build_message(
        repo="watcher-cog",
        flow_name="dj-sets",
        run_id="r1",
        severity="SUCCESS",
        text="x",
        source="watcher_loop",
        dimension="cd_readiness",
        suggestion=None,
    )
    assert message["embeds"][0]["title"].startswith("[DEVELOPMENT] ")


def test_build_message_leaves_production_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    message = ps._build_message(
        repo="watcher-cog",
        flow_name="dj-sets",
        run_id="r1",
        severity="SUCCESS",
        text="x",
        source="watcher_loop",
        dimension="cd_readiness",
        suggestion=None,
    )
    assert message["embeds"][0]["title"] == "watcher-cog · dj-sets"


def test_delivery_uses_notify_not_evaluations(monkeypatch) -> None:
    """Regression: run status goes to /v1/notify, never back to findings.

    The whole point of the move is that a run outcome is not a graded
    finding. If this ever calls post('/v1/evaluations', ...) again, the
    evaluations table starts collecting telemetry a second time.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    client = MagicMock()
    with patch(
        "mini_app_polis.api.KaianoApiClient.from_env", return_value=client
    ) as from_env:
        ps.post_run_finding("f", "WARN", text="x", repo="my-cog")

    from_env.assert_called_once_with(machine_name="my-cog")
    client.notify.assert_called_once()
    client.post.assert_not_called()
    assert client.notify.call_args.kwargs["embeds"]


def test_dimension_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="x", repo="my-cog", dimension="freshness")
    assert _built(cap)["dimension"] == "freshness"


def test_explicit_source_is_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="bad", repo="my-cog", source="flow_hook")
    assert _built(cap)["source"] == "flow_hook"


# ---------------------------------------------------------------------------
# Severity handling
# ---------------------------------------------------------------------------


def test_success_is_logged_not_notified(monkeypatch) -> None:
    """The noise control: a successful run says nothing in the channel.

    Absence is what Healthchecks.io watches, so a green run needs no
    message. If this regresses, every scheduled flow in the fleet starts
    announcing itself.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    mock_log = MagicMock()
    with (
        captured() as cap,
        patch.object(ps, "get_prefect_logger", return_value=mock_log),
    ):
        result = ps.post_run_finding("f", "SUCCESS", repo="my-cog")

    cap.deliver.assert_not_called()
    assert result.suppressed == 1
    assert result.sent == 0
    mock_log.info.assert_called()
    assert "Run completed successfully." in mock_log.info.call_args.args


def test_success_not_in_notify_severities() -> None:
    """The policy itself, stated once."""
    assert "SUCCESS" not in ps.NOTIFY_SEVERITIES
    assert {"WARN", "ERROR", "CRITICAL"} <= ps.NOTIFY_SEVERITIES


def test_warn_and_error_are_notified(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    for severity in ("WARN", "ERROR", "CRITICAL"):
        with captured() as cap:
            result = ps.post_run_finding("f", severity, text="x", repo="my-cog")
        assert result.sent == 1, severity
        assert _built(cap)["severity"] == severity


# ---------------------------------------------------------------------------
# Extras → text suffix
# ---------------------------------------------------------------------------


def test_nonzero_extras_appended_to_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding(
            "f", "WARN", text="Issues", repo="my-cog", ingest_attempted=1
        )
    assert _built(cap)["text"] == "Issues ingest_attempted=1"


def test_zero_extras_omitted_from_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding(
            "f", "WARN", text="Issues", repo="my-cog", ingest_attempted=0
        )
    assert _built(cap)["text"] == "Issues"


def test_multiple_extras_sorted_alphabetically(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding(
            "f", "WARN", text="Issues", repo="my-cog", zebra=3, alpha=1, mid=2
        )
    assert _built(cap)["text"] == "Issues alpha=1; mid=2; zebra=3"


# ---------------------------------------------------------------------------
# Best-effort, and truthful about it
# ---------------------------------------------------------------------------


def test_swallows_delivery_exception(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured(deliver_raises=RuntimeError("boom")):
        result = ps.post_run_finding("f", "WARN", text="x", repo="my-cog")
    assert result.failed == 1
    assert result.sent == 0
    assert result.ok is False


def test_failed_delivery_is_not_reported_as_sent(monkeypatch) -> None:
    """The September lesson: never claim success you did not get."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured(deliver_returns=False):
        result = ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[
                {"severity": "WARN", "finding": "a"},
                {"severity": "ERROR", "finding": "b"},
            ],
        )
    assert result.sent == 0
    assert result.failed == 2
    assert result.ok is False


def test_delivery_failure_reaches_sentry(monkeypatch) -> None:
    """Layer three gets the exception, whether or not anyone reads the log."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    captured_exc: list[BaseException] = []
    monkeypatch.setattr(ps, "_capture", captured_exc.append)
    with captured(deliver_raises=RuntimeError("boom")):
        ps.post_run_finding("f", "WARN", text="x", repo="my-cog")
    assert len(captured_exc) == 1
    assert isinstance(captured_exc[0], RuntimeError)


def test_capture_is_safe_without_sentry_installed() -> None:
    """_capture must be callable in a process that has no sentry_sdk.

    sentry_sdk is not a dependency of this library — the mp3 and Google
    helpers should not start paying for one — so the import failing is
    the ordinary case, not the exceptional one.
    """
    with patch.dict("sys.modules", {"sentry_sdk": None}):
        assert ps._capture(RuntimeError("x")) is None


def test_capture_reports_when_sentry_is_present() -> None:
    """And the other half: it does report when the host process has it.

    Without this, a version of _capture that returned early every time
    would pass the test above and quietly report nothing anywhere.
    """
    sentry = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": sentry}):
        ps._capture(RuntimeError("boom"))

    sentry.capture_exception.assert_called_once()


# ---------------------------------------------------------------------------
# make_failure_hook
# ---------------------------------------------------------------------------


def test_failure_hook_crashed_reports_error(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog")
    state = SimpleNamespace(name="Crashed", type="CRASHED")
    with captured() as cap:
        hook(None, None, state)
    built = _built(cap)
    assert built["severity"] == "ERROR"
    assert built["source"] == "flow_hook"
    assert built["repo"] == "my-cog"


def test_failure_hook_failed_reports_warn(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog")
    state = SimpleNamespace(name="Failed", type="FAILED")
    with captured() as cap:
        hook(None, None, state)
    assert _built(cap)["severity"] == "WARN"


def test_failure_hook_production_only_false_no_delivery(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog", production_only=False)
    state = SimpleNamespace(name="Failed", type="FAILED")
    with captured() as cap:
        hook(None, None, state)
    cap.deliver.assert_not_called()


def test_failure_hook_swallows_post_run_finding_exception(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    hook = ps.make_failure_hook("fl", repo="my-cog")
    state = SimpleNamespace(name="Failed", type="FAILED")
    mock_log = MagicMock()
    with (
        patch.object(ps, "post_run_finding", side_effect=RuntimeError("x")),
        patch.object(ps, "get_prefect_logger", return_value=mock_log),
    ):
        hook(None, None, state)
    mock_log.exception.assert_called()


# ---------------------------------------------------------------------------
# post_findings — the findings path, still writing rows
# ---------------------------------------------------------------------------
#
# These tests exist to keep run status from creeping back in here. The
# evaluator delivers its graded conformance findings through this funnel,
# so it must keep POSTing to /v1/evaluations; pointing it anywhere else
# stops the conformance record accumulating and nothing fails loudly.


def test_post_findings_posts_each_row(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        result = ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "first issue"},
                {"severity": "WARN", "finding": "second issue"},
                {"severity": "ERROR", "finding": "third issue"},
            ],
        )
    assert post.call_count == 3
    assert result.sent == 3
    posted = [c.args[0]["finding"] for c in post.call_args_list]
    assert posted == ["first issue", "second issue", "third issue"]


def test_post_findings_goes_to_evaluations_not_notify(monkeypatch) -> None:
    """Regression: graded findings are records and must stay records.

    The evaluator's conformance output travels this funnel. If it ever
    starts calling notify(), the findings table stops filling and the only
    symptom is a burn-down that quietly flattens.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    client = MagicMock()
    with patch(
        "mini_app_polis.api.KaianoApiClient.from_env", return_value=client
    ) as from_env:
        ps.post_findings(
            repo="evaluator-cog",
            flow_name="conformance-check",
            findings=[{"severity": "WARN", "finding": "CD-019 violated"}],
        )
    from_env.assert_called_once_with(machine_name="evaluator-cog")
    client.post.assert_called_once()
    assert client.post.call_args.args[0] == "/v1/evaluations"
    client.notify.assert_not_called()


def test_post_findings_does_not_suppress_success(monkeypatch) -> None:
    """A SUCCESS finding is a graded result, not a heartbeat."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        result = ps.post_findings(
            repo="evaluator-cog",
            flow_name="conformance-check",
            findings=[{"severity": "SUCCESS", "finding": "CD-019 satisfied"}],
        )
    post.assert_called_once()
    assert result.sent == 1
    assert post.call_args.args[0]["severity"] == "SUCCESS"


def test_post_findings_shared_fields_applied_to_every_row(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("PREFECT_FLOW_RUN_ID", "abc-123")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "a"},
                {"severity": "ERROR", "finding": "b"},
            ],
            source="flow_hook",
        )
    for call in post.call_args_list:
        payload = call.args[0]
        assert payload["repo"] == "my-cog"
        assert payload["flow_name"] == "my-flow"
        assert payload["source"] == "flow_hook"
        assert payload["run_id"] == "abc-123"


def test_post_findings_omits_standards_version(monkeypatch) -> None:
    """Self-reported rows must not carry a standards revision they lack."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("STANDARDS_VERSION", "6.0")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[{"severity": "WARN", "finding": "a"}],
        )
    assert "standards_version" not in post.call_args.args[0]


def test_post_findings_per_row_dimension_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "a", "dimension": "data_quality"},
                {"severity": "ERROR", "finding": "b"},
            ],
        )
    assert post.call_args_list[0].args[0]["dimension"] == "data_quality"
    assert post.call_args_list[1].args[0]["dimension"] == "pipeline_consistency"


def test_post_findings_suggestion_passed_through(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
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
        )
    assert post.call_args.args[0]["suggestion"] == "do the thing differently"


def test_post_findings_suggestion_omitted_when_none(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[{"severity": "WARN", "finding": "a"}],
        )
    assert "suggestion" not in post.call_args.args[0]


def test_post_findings_reports_failures_truthfully(monkeypatch) -> None:
    """The September lesson, on the path it actually happened to."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=False):
        result = ps.post_findings(
            repo="evaluator-cog",
            flow_name="conformance-check",
            findings=[
                {"severity": "WARN", "finding": "a"},
                {"severity": "WARN", "finding": "b"},
            ],
        )
    assert result.sent == 0
    assert result.failed == 2
    assert result.ok is False


def test_post_findings_per_row_error_isolation(monkeypatch) -> None:
    """One row's POST failure does not prevent later rows being attempted."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    seen: list[dict] = []

    def fake_post(payload):
        seen.append(payload)
        if payload["finding"] == "boom":
            raise RuntimeError("API exploded")
        return True

    with patch.object(ps, "_post_evaluation", side_effect=fake_post):
        result = ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "first"},
                {"severity": "ERROR", "finding": "boom"},
                {"severity": "WARN", "finding": "third"},
            ],
        )
    assert [p["finding"] for p in seen] == ["first", "boom", "third"]
    assert result.sent == 2
    assert result.failed == 1


def test_post_findings_skips_empty_finding_text(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        result = ps.post_findings(
            repo="my-cog",
            flow_name="my-flow",
            findings=[
                {"severity": "WARN", "finding": "   "},
                {"severity": "WARN", "finding": ""},
                {"severity": "WARN", "finding": "valid"},
            ],
        )
    assert post.call_count == 1
    assert post.call_args.args[0]["finding"] == "valid"
    assert result.skipped == 2


def test_post_findings_empty_batch_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with patch.object(ps, "_post_evaluation") as post:
        result = ps.post_findings(repo="my-cog", flow_name="my-flow", findings=[])
    post.assert_not_called()
    assert result == ps.DeliveryReport()


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
        )
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Processor version stamping
# ---------------------------------------------------------------------------
#
# The library appends ``(processor=X.Y.Z)`` to every report text so a
# person reading the channel knows which build emitted it. Stamping
# happens at the post_findings funnel so every cog routing through the
# library gets it uniformly. The previous per-cog approach stamped
# ``(processor=0.0.0+local)`` in production because it queried the
# pre-merge distribution name ``"voicenotes-cog"``; centralising the
# resolution and treating "not installed" as "no suffix" prevents that
# class of regression.
#
# ``_resolve_processor_version`` is cached on ``repo``, so every test in
# this block clears the cache to keep monkeypatches honest.


def _reset_version_cache() -> None:
    ps._resolve_processor_version.cache_clear()


def test_processor_version_appended_when_resolvable(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="Run degraded.", repo="my-cog")
    assert _built(cap)["text"] == "Run degraded. (processor=1.2.3)"


def test_processor_version_omitted_when_not_installed(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()

    def _missing(name: str) -> str:
        raise ps.PackageNotFoundError(name)

    monkeypatch.setattr(ps, "version", _missing)
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="Run degraded.", repo="my-cog")
    assert _built(cap)["text"] == "Run degraded."


def test_processor_version_swallows_unexpected_errors(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()

    def _boom(name: str) -> str:
        raise RuntimeError("broken metadata")

    monkeypatch.setattr(ps, "version", _boom)
    with captured() as cap:
        ps.post_run_finding("f", "WARN", text="Run degraded.", repo="my-cog")
    assert _built(cap)["text"] == "Run degraded."


def test_processor_version_not_double_stamped(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[
                {"severity": "WARN", "finding": "Already labeled (processor=9.9.9)"}
            ],
        )
    assert post.call_args.args[0]["finding"] == "Already labeled (processor=9.9.9)"


def test_processor_version_applied_to_every_row(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="f",
            findings=[
                {"severity": "WARN", "finding": "first"},
                {"severity": "ERROR", "finding": "second"},
            ],
        )
    texts = [c.args[0]["finding"] for c in post.call_args_list]
    assert texts == ["first (processor=1.2.3)", "second (processor=1.2.3)"]


def test_processor_version_cached_per_repo(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    calls: list[str] = []

    def _counting(name: str) -> str:
        calls.append(name)
        return "1.2.3"

    monkeypatch.setattr(ps, "version", _counting)
    with captured():
        ps.post_run_finding("f", "WARN", text="x", repo="my-cog")
        ps.post_run_finding("f", "WARN", text="x", repo="my-cog")
        ps.post_run_finding("f", "WARN", text="x", repo="other-cog")
    assert sorted(calls) == ["my-cog", "other-cog"]


def test_processor_version_stamped_on_suppressed_success(monkeypatch) -> None:
    """SUCCESS goes to the log, and the log line carries the build too."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    mock_log = MagicMock()
    with (
        captured(),
        patch.object(ps, "get_prefect_logger", return_value=mock_log),
    ):
        ps.post_run_finding("f", "SUCCESS", text="All good.", repo="my-cog")
    assert "All good. (processor=1.2.3)" in mock_log.info.call_args.args


def test_processor_version_failure_hook_stamps(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    monkeypatch.setattr(ps, "version", lambda _: "1.2.3")
    hook = ps.make_failure_hook("fl", repo="my-cog")
    state = SimpleNamespace(name="Failed", type="FAILED")
    with captured() as cap:
        hook(None, None, state)
    assert "(processor=1.2.3)" in _built(cap)["text"]


# ---------------------------------------------------------------------------
# CRITICAL severity
# ---------------------------------------------------------------------------
#
# CRITICAL is for the pre-flow process lifecycle only —
# serve_resilience.serve_with_retry emits it with source="startup" when a
# cog cannot register its deployments and is exiting. These tests pin the
# two properties that matter: CRITICAL is carried verbatim (no downgrade,
# the same class of bug as the old evaluator-cog SUCCESS→WARN downgrade),
# and it stays scoped to startup — the flow-run paths cap at ERROR.


def test_critical_severity_preserved(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    with captured() as cap:
        ps.post_run_finding(
            "startup",
            "CRITICAL",
            text="Registration failed",
            repo="my-cog",
            source="startup",
        )
    built = _built(cap)
    assert built["severity"] == "CRITICAL"
    assert built["source"] == "startup"


def test_critical_in_severity_literal() -> None:
    """The Literal itself is the contract consumers type-check against."""
    from typing import get_args

    assert set(get_args(ps.Severity)) == {"SUCCESS", "WARN", "ERROR", "CRITICAL"}


def test_critical_available_via_post_findings_batch(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    _reset_version_cache()
    with patch.object(ps, "_post_evaluation", return_value=True) as post:
        ps.post_findings(
            repo="my-cog",
            flow_name="startup",
            findings=[{"severity": "CRITICAL", "finding": "process is exiting"}],
            source="startup",
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
    hook = ps.make_failure_hook("fl", repo="my-cog")
    for state in (
        SimpleNamespace(name="Crashed", type="CRASHED"),
        SimpleNamespace(name="Failed", type="FAILED"),
        SimpleNamespace(name="Weird", type="SOMETHING_ELSE"),
    ):
        with captured() as cap:
            hook(None, None, state)
        assert _built(cap)["severity"] in {"WARN", "ERROR"}


# ---------------------------------------------------------------------------
# notable — a triggered run reports even when it produced nothing
# ---------------------------------------------------------------------------


def test_notable_success_is_sent(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        result = ps.post_run_finding(
            "f", "SUCCESS", text="3 files ingested", repo="my-cog", notable=True
        )
    assert result.sent == 1
    assert _built(cap)["severity"] == "SUCCESS"


def test_notable_success_with_no_work_is_still_sent(monkeypatch) -> None:
    """The case the flag exists for.

    A run that fired because a condition was met and then processed
    nothing has all-zero counters. Inferring "worth reporting" from output
    would silence exactly the run that most needs explaining.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        result = ps.post_run_finding(
            "f",
            "SUCCESS",
            text="Triggered by 2 new files; none matched the filter",
            repo="my-cog",
            notable=True,
            files_processed=0,
        )
    assert result.sent == 1
    assert "none matched" in _built(cap)["text"]


def test_idle_tick_stays_silent(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        result = ps.post_run_finding("f", "SUCCESS", repo="my-cog")
    cap.deliver.assert_not_called()
    assert result.suppressed == 1


def test_notable_does_not_override_gating(monkeypatch) -> None:
    """notable says 'worth reporting', not 'ignore where we are running'."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    with captured() as cap:
        ps.post_run_finding(
            "f", "SUCCESS", text="x", repo="my-cog", notable=True, production_only=False
        )
    cap.deliver.assert_not_called()
