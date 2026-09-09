"""RunReport: what it counts, what it says, and when it sends.

The delivery tests patch ``post_run_finding`` rather than the transport.
What matters here is the call this object composes — severity, text,
extras — not whether the module underneath it can reach Discord, which
is already covered where that module is tested.
"""

from __future__ import annotations

import pytest

from mini_app_polis import pipeline_status as ps


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


def test_domain_counters_do_not_raise_severity():
    r = ps.RunReport(flow_name="f", repo="c")
    r.count("tracks", 412)
    assert r.severity == "SUCCESS"


# ── Text ────────────────────────────────────────────────────────────────


def test_empty_run_says_so():
    assert ps.RunReport(flow_name="f", repo="c").text() == (
        "Run complete — nothing to do."
    )


def test_tally_is_ordered_issues_before_notes():
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok(12)
    r.note("already_processed")
    r.issue("invalid_filename")
    r.issue("invalid_filename")
    assert r.tally() == "processed=12, invalid_filename=2, already_processed=1"


def test_only_flagged_reasons_name_names():
    r = ps.RunReport(flow_name="f", repo="c")
    r.note("already_processed", "boring.txt")
    r.issue("invalid_filename", "bad.txt")
    text = r.text()
    assert "invalid_filename: bad.txt" in text
    assert "boring.txt" not in text


def test_examples_are_capped_and_the_rest_counted():
    r = ps.RunReport(flow_name="f", repo="c")
    for i in range(7):
        r.issue("bad", f"f{i}.txt")
    line = [ln for ln in r.text().splitlines() if ln.startswith("bad:")][0]
    assert line == "bad: f0.txt, f1.txt, f2.txt, +4 more"


def test_detail_is_carried_with_the_item():
    r = ps.RunReport(flow_name="f", repo="c")
    r.issue("ingest_failed", "2025-01-02 Venue", detail="422 unprocessable")
    assert "422 unprocessable" in r.text()


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
    assert "empty_sheet: Jan 3" in call["text"]


def test_send_is_idempotent(sent):
    r = ps.RunReport(flow_name="f", repo="c")
    r.ok()
    r.send()
    r.send()
    assert len(sent) == 1


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
    assert "processed=4" in call["text"]
    assert "bad_row" in call["text"]
    assert "unhandled_exception" in call["text"]
    assert "ValueError" in call["text"]


def test_production_only_is_passed_through(sent):
    with ps.run_report("f", repo="c", production_only=False) as r:
        r.ok()
    assert sent[0]["production_only"] is False
