"""RunReport: what it counts, what it says, and when it sends.

The delivery tests patch ``post_run_finding`` rather than the transport.
What matters here is the call this object composes — severity, text,
extras — not whether the module underneath it can reach Discord, which
is already covered where that module is tested.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mini_app_polis import pipeline_status as ps


def _headline(report: ps.RunReport) -> str:
    return report.text().splitlines()[0]


@pytest.fixture
def sent(monkeypatch):
    """Capture the post_run_finding call this object composes."""
    calls: list[dict] = []

    def _fake(flow_name, severity, **kwargs):
        calls.append({"flow_name": flow_name, "severity": severity, **kwargs})
        return ps.DeliveryReport(sent=1)

    monkeypatch.setattr(ps, "post_run_finding", _fake)
    return calls


# ── Severity ────────────────────────────────────────────────────────────


def test_clean_run_is_success():
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(5)
    assert r.severity == "SUCCESS"


def test_ordinary_skips_do_not_raise_severity():
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(5)
    r.note("already_processed", "a.txt")
    assert r.severity == "SUCCESS"


def test_one_issue_makes_the_run_warn():
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(5)
    r.issue("invalid_filename", "b.txt")
    assert r.severity == "WARN"


def test_a_notice_does_not_raise_severity():
    r = ps.RunReport(flow_name="f", repo="c")
    r.notice("task newly held", "TASK-2")
    assert r.severity == "SUCCESS"


def test_domain_counters_do_not_raise_severity():
    r = ps.RunReport(flow_name="f", repo="c")
    r.count("tracks", 412)
    assert r.severity == "SUCCESS"


def test_outcomes_do_not_raise_severity():
    """Creating things is what a healthy run does."""
    r = ps.RunReport(flow_name="f", repo="c")
    r.created("asana task", "Fix the importer")
    r.removed("recording", "old.m4a")
    assert r.severity == "SUCCESS"


def test_an_issue_alongside_outcomes_still_warns():
    r = ps.RunReport(flow_name="f", repo="c")
    r.created("asana task", "Fix the importer")
    r.issue("transcribe_failed", "note.m4a")
    assert r.severity == "WARN"


# ── Text ────────────────────────────────────────────────────────────────


def test_empty_run_says_so():
    assert _headline(ps.RunReport(flow_name="f", repo="c", duration_sec=0.4)) == (
        "Run complete in 0.40s — nothing to do."
    )


def test_a_run_with_only_outcomes_is_not_called_empty():
    r = ps.RunReport(flow_name="f", repo="c", duration_sec=0.4)
    r.updated("grooming proposal", "TASK-129")
    assert r.text().splitlines() == [
        "Run complete in 0.40s.",
        "",
        "grooming proposal",
        "  ~ TASK-129",
    ]


def test_the_tally_counts_in_words_never_name_equals_value():
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(12)
    r.note("already_processed")
    r.issue("invalid_filename")
    r.issue("invalid_filename")
    assert r.tally() == "12 processed, 2 to look at"
    assert "=" not in r.text()


def test_only_flagged_reasons_name_names():
    r = ps.RunReport(flow_name="f", repo="c")
    r.note("already_processed", "boring.txt")
    r.issue("invalid_filename", "bad.txt")
    text = r.text()
    assert "invalid_filename\n  bad.txt" in text
    assert "boring.txt" not in text
    assert "Skipped\n  1 already_processed" in text


def test_a_notice_names_its_items_before_the_issues():
    r = ps.RunReport(flow_name="f", repo="c", duration_sec=0.4)
    r.issue("invalid_filename", "bad.txt")
    r.notice("task newly held", "TASK-2")
    r.notice("task newly held", "TASK-3")
    assert r.text().splitlines() == [
        "Run complete in 0.40s — 1 to look at.",
        "",
        "task newly held",
        "  TASK-2",
        "  TASK-3",
        "",
        "invalid_filename",
        "  bad.txt",
    ]


def test_examples_are_capped_and_the_rest_counted():
    r = ps.RunReport(flow_name="f", repo="c")
    for i in range(7):
        r.issue("bad", f"f{i}.txt")
    assert r.text().split("\n\n")[1].splitlines() == [
        "bad",
        "  f0.txt",
        "  f1.txt",
        "  f2.txt",
        "  4 more",
    ]


def test_detail_is_carried_with_the_item():
    r = ps.RunReport(flow_name="f", repo="c")
    r.issue("ingest_failed", "2025-01-02 Venue", detail="422 unprocessable")
    assert "422 unprocessable" in r.text()


# ── Outcomes ────────────────────────────────────────────────────────────


def test_what_the_run_made_is_in_the_message():
    """The regression this verb exists for.

    A voice note became an Asana task and the report said "1 file(s)
    seen". The id was computed, returned, and dropped one frame before
    anything reached Discord.
    """
    r = ps.RunReport(flow_name="voicenotes-ingest", repo="transcription-cog")
    r.ok(1)
    r.created("asana task", "Fix the DJ set importer")
    assert "asana task\n  + Fix the DJ set importer" in r.text()


def test_a_link_makes_the_item_clickable():
    r = ps.RunReport(flow_name="f", repo="c")
    r.created("asana task", "Fix it", link="https://tasks.example.com/1")
    assert "  + [Fix it](https://tasks.example.com/1)" in r.text()


def test_operations_render_in_a_fixed_order_whatever_order_they_happened():
    r = ps.RunReport(flow_name="f", repo="c")
    r.removed("page", "gone")
    r.updated("page", "changed")
    r.created("page", "new")
    marks = [line.strip()[0] for line in r.outcome_lines() if line.startswith("  ")]
    assert marks == ["+", "~", "-"]


def test_outcomes_come_before_problems():
    r = ps.RunReport(flow_name="f", repo="c")
    r.issue("bad_row", "row 9")
    r.created("dj set", "2026-01-02 Venue")
    sections = r.text().split("\n\n")
    assert sections[1] == "dj set\n  + 2026-01-02 Venue"
    assert sections[2] == "bad_row\n  row 9"


def test_same_kind_groups_under_one_label_one_item_a_line_and_caps():
    r = ps.RunReport(flow_name="f", repo="c")
    for i in range(8):
        r.created("wiki page", f"page-{i}")
    assert r.outcome_lines() == [
        "wiki page",
        *(f"  + page-{i}" for i in range(5)),
        "  + 3 more",
    ]


def test_kinds_are_separate_lines_and_sorted():
    r = ps.RunReport(flow_name="f", repo="c")
    r.created("wiki page", "a")
    r.created("asana task", "b")
    assert r.outcome_lines() == ["asana task", "  + b", "", "wiki page", "  + a"]


def test_an_unnamed_outcome_still_counts():
    """Some runs know they wrote four things without a name for each."""
    r = ps.RunReport(flow_name="f", repo="c")
    for _ in range(4):
        r.created("sheet row")
    assert r.outcome_lines() == ["sheet row", "  + 4"]


# ── Duration ────────────────────────────────────────────────────────────


def test_the_headline_says_how_long_it_took():
    r = ps.RunReport(flow_name="f", repo="c", duration_sec=12.43)
    r.ok(1)
    assert _headline(r) == "Run complete in 12.4s — 1 processed."


def test_a_supplied_duration_wins_over_the_measured_one():
    """A report built after the fact — from a crash hook — knows better."""
    r = ps.RunReport(flow_name="f", repo="c", duration_sec=90.0)
    assert r.duration == 90.0
    assert "1m 30s" in _headline(r)


def test_an_unsupplied_duration_is_measured(monkeypatch):
    ticks = iter([100.0, 104.5])
    monkeypatch.setattr(ps, "_now", lambda: next(ticks))
    r = ps.RunReport(flow_name="f", repo="c")
    assert r.duration == pytest.approx(4.5)


def test_the_clock_stops_when_the_report_is_sent(monkeypatch, sent):
    """text() after send() must not keep counting."""
    ticks = iter([100.0, 103.0, 500.0])
    monkeypatch.setattr(ps, "_now", lambda: next(ticks))
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok()
    r.send()
    assert "3.0s" in sent[0]["text"]
    assert r.duration == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [
        (0.04, "0.04s"),
        (0.999, "1.00s"),
        (3.25, "3.2s"),
        (59.9, "59.9s"),
        (60, "1m 00s"),
        (187, "3m 07s"),
        (3600, "1h 00m"),
        (4021, "1h 07m"),
    ],
)
def test_durations_read_the_way_a_person_reads_one(seconds, rendered):
    assert ps.format_duration(seconds) == rendered


# ── Delivery ────────────────────────────────────────────────────────────


def test_send_composes_the_call(sent):
    r = ps.RunReport(flow_name="update-dj-set-collection", repo="deejay-cog")
    r.ok(3)
    r.count("tracks", 412)
    r.issue("empty_sheet", "Jan 3")
    r.send(notable=True)

    assert len(sent) == 1
    call = sent[0]
    assert call["flow_name"] == "update-dj-set-collection"
    assert call["severity"] == "WARN"
    assert call["repo"] == "deejay-cog"
    assert call["tracks"] == 412
    assert call["notable"] is True
    assert "empty_sheet\n  Jan 3" in call["text"]


def test_send_is_idempotent(sent):
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok()
    r.send()
    r.send()
    assert len(sent) == 1


def test_an_outcome_makes_a_success_run_notable(sent):
    """The silence this closes: SUCCESS is suppressed unless notable, so a
    run that created an Asana task went to the log and nowhere else."""
    r = ps.RunReport(flow_name="voicenotes-ingest", repo="transcription-cog")
    r.ok(1)
    r.created("asana task", "Fix the DJ set importer")
    r.send()
    assert sent[0]["severity"] == "SUCCESS"
    assert sent[0]["notable"] is True


def test_a_run_with_nothing_to_show_is_still_the_caller_s_call(sent):
    """A triggered run that did nothing has no outcome to speak for it."""
    r = ps.RunReport(flow_name="f", repo="c")
    r.send()
    assert sent[0]["notable"] is False

    r2 = ps.RunReport(flow_name="f", repo="c")
    r2.send(notable=True)
    assert sent[1]["notable"] is True


def test_a_notice_makes_a_success_run_notable(sent):
    r = ps.RunReport(flow_name="f", repo="c")
    r.notice("task newly held", "TASK-2")
    r.send()
    assert sent[0]["severity"] == "SUCCESS"
    assert sent[0]["notable"] is True


def test_counted_work_alone_is_not_an_outcome(sent):
    """``ok(n)`` is a tally, not a claim that anything now exists."""
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(5)
    r.count("tracks", 412)
    r.send()
    assert sent[0]["notable"] is False


# ── Context manager ─────────────────────────────────────────────────────


def test_block_sends_on_the_way_out(sent):
    with ps.run_report("f", repo="c") as r:
        r.ok(2)
    assert len(sent) == 1
    assert sent[0]["severity"] == "SUCCESS"


def test_explicit_send_inside_the_block_is_not_doubled(sent):
    with ps.run_report("f", repo="c") as r:
        r.ok()
        r.send()
    assert len(sent) == 1


def test_exception_is_reported_with_what_the_run_had_done_and_re_raised(sent):
    with pytest.raises(ValueError, match="halfway"), ps.run_report("f", repo="c") as r:
        r.ok(4)
        r.issue("bad_row", "row 9")
        raise ValueError("halfway")

    assert len(sent) == 1
    call = sent[0]
    assert call["severity"] == "WARN"
    assert call["notable"] is True
    assert "4 processed" in call["text"]
    assert "bad_row" in call["text"]
    assert "unhandled_exception" in call["text"]
    assert "ValueError" in call["text"]


def test_running_out_of_time_is_a_planned_stop_said_once(sent):
    class RunOutOfTime(BaseException):  # noqa: N818 — the worker's name
        pass

    with (
        pytest.raises(RunOutOfTime),
        ps.run_report("groom-sweep", repo="task-manager-cog") as r,
    ):
        r.ok(3)
        r.duration_sec = 97
        raise RunOutOfTime("stopped 20s before the function timeout")

    (call,) = sent
    assert call["severity"] == "WARN"
    assert call["text"].splitlines()[0] == (
        "Stopped early after 1m 37s: out of time (stopped 20s before the "
        "function timeout); the next run picks up the rest."
    )
    assert "unhandled_exception" not in call["text"]
    assert ps.STOPPED_EARLY not in call["text"]


def test_counters_carry_their_labels_and_sections(sent):
    r = ps.RunReport(flow_name="f", repo="c")
    r.count("tasks_checked", 42, label="tasks checked", section="Board")
    r.count("agent_spend_usd", "0.10", label="${} spent", section="Agent")
    r.count("plain", 3)
    r.send()
    (call,) = sent
    assert call["counter_labels"] == {
        "tasks_checked": "tasks checked",
        "agent_spend_usd": "${} spent",
    }
    assert call["counter_sections"] == {
        "tasks_checked": "Board",
        "agent_spend_usd": "Agent",
    }
    assert call["tasks_checked"] == 42


def test_production_only_is_passed_through(sent):
    with ps.run_report("f", repo="c", production_only=False) as r:
        r.ok()
    assert sent[0]["production_only"] is False


# ---------------------------------------------------------------------------
# run identity
# ---------------------------------------------------------------------------


def test_report_sends_under_the_run_id_it_was_given() -> None:
    """Otherwise the report is unattributable to the findings it describes."""
    report = ps.RunReport(
        flow_name="conformance-check",
        repo="evaluator-cog",
        run_id="deterministic-7.0.0-abc123",
    )
    report.issue("repo_download_failed", "watcher-cog")

    with patch.object(ps, "post_run_finding") as post:
        report.send()

    assert post.call_args.kwargs.get("run_id") == "deterministic-7.0.0-abc123"


def test_run_id_may_be_set_after_construction() -> None:
    """The id is often minted from data the report was opened to collect.

    evaluator-cog builds its run id from the standards catalog version,
    which it fetches using the same context that carries the report — so
    the report necessarily exists before the id does.
    """
    report = ps.RunReport(flow_name="conformance-check", repo="evaluator-cog")
    assert report.run_id is None

    report.run_id = "deterministic-7.0.0-late"
    with patch.object(ps, "post_run_finding") as post:
        report.send()

    assert post.call_args.kwargs.get("run_id") == "deterministic-7.0.0-late"


def test_a_report_with_no_run_id_leaves_the_fallback_in_charge() -> None:
    """Unconverted cogs keep whatever get_run_id resolves for them."""
    report = ps.RunReport(flow_name="f", repo="some-cog")
    with patch.object(ps, "post_run_finding") as post:
        report.send()

    assert post.call_args.kwargs.get("run_id") is None
