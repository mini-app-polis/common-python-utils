"""Per-invocation timing: idle vs working, and what the waiting was on."""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator

import httplib2
import httpx
import pytest
import urllib3

from mini_app_polis import timing


@pytest.fixture
def lines() -> list[dict]:
    return []


@pytest.fixture(autouse=True)
def _off_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    """No memory size and no cgroup counters, whatever runs the tests."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", raising=False)
    monkeypatch.setattr(timing, "_CGROUP_THROTTLE", ())


def _run(lines: list[dict], **labels: object):
    return timing.invocation(emit=lambda s: lines.append(json.loads(s)), **labels)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    """A local HTTP server that answers every GET after a short pause."""

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # Not time.sleep: the server's thread is not the one being timed.
            timing._real_sleep(0.05)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.mark.parametrize(
    ("host", "category"),
    [
        ("www.googleapis.com", "google"),
        ("oauth2.googleapis.com", "google"),
        ("accounts.spotify.com", "spotify"),
        ("api.anthropic.com", "anthropic"),
        ("app.asana.com", "asana"),
        ("ssm.us-east-1.amazonaws.com", "aws"),
        ("api.kaianolevine.com", "api"),
        ("notspotify.com", "notspotify.com"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_category_for(host, category) -> None:
    assert timing.category_for(host) == category


def test_one_line_with_labels_however_the_block_ends(lines) -> None:
    with pytest.raises(RuntimeError), _run(lines, cog="deejay") as inv:
        inv.label(mode="process-new-files")
        raise RuntimeError("boom")

    assert len(lines) == 1
    record = lines[0]["timing"]
    assert record["labels"] == {"cog": "deejay", "mode": "process-new-files"}
    assert set(record) == {
        "wall_ms",
        "cpu_ms",
        "idle_pct",
        "wait",
        "unattributed_ms",
        "labels",
    }


def test_sleep_is_waiting_on_sleep(lines) -> None:
    with _run(lines):
        time.sleep(0.05)

    record = lines[0]["timing"]
    assert record["wait"]["sleep"]["calls"] == 1
    assert record["wait"]["sleep"]["ms"] >= 45
    assert record["idle_pct"] > 50


def test_nested_waits_count_each_second_once(lines) -> None:
    with _run(lines), timing.waiting("google"):
        time.sleep(0.05)  # sleep, not google
        with timing.waiting("google"):  # a retry inside the same client
            time.sleep(0.0)

    wait = lines[0]["timing"]["wait"]
    assert wait["sleep"]["ms"] >= 45
    assert wait["google"]["ms"] < 30
    assert wait["google"]["calls"] == 1


def test_httpx_calls_are_filed_by_host(lines) -> None:
    def slow(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.03)
        return httpx.Response(200, json={})

    with _run(lines), httpx.Client(transport=httpx.MockTransport(slow)) as client:
        client.get("https://api.anthropic.com/v1/messages")
        client.get("https://api.kaianolevine.com/v1/sets")

    wait = lines[0]["timing"]["wait"]
    assert wait["anthropic"]["calls"] == 1
    assert wait["api"]["calls"] == 1
    # The transport's own sleep happens inside the call: it is counted once,
    # under sleep, and the call keeps only what is left.
    assert wait["sleep"]["calls"] == 2


def test_the_llm_sdks_transport_is_timed(lines) -> None:
    """The Anthropic and OpenAI SDKs send through httpx2, not httpx."""
    httpx2 = pytest.importorskip("httpx2")

    def slow(_request):
        timing._real_sleep(0.03)
        return httpx2.Response(200, json={})

    with _run(lines), httpx2.Client(transport=httpx2.MockTransport(slow)) as client:
        client.post("https://api.anthropic.com/v1/messages")

    entry = lines[0]["timing"]["wait"]["anthropic"]
    assert entry["calls"] == 1
    assert entry["ms"] >= 25


def test_urllib3_and_httplib2_calls_are_timed(lines, server) -> None:
    with _run(lines):
        urllib3.PoolManager().request("GET", server)
        httplib2.Http().request(server)

    entry = lines[0]["timing"]["wait"]["127.0.0.1"]
    assert entry["calls"] == 2
    assert entry["ms"] >= 90


def test_nothing_is_recorded_outside_an_invocation(lines) -> None:
    time.sleep(0.01)
    with timing.waiting("google"):
        pass
    with _run(lines):
        pass

    assert lines[0]["timing"]["wait"] == {}


def test_a_failing_emit_never_fails_the_run() -> None:
    def broken(_line: str) -> None:
        raise OSError("stdout closed")

    with timing.invocation(emit=broken):
        pass


def test_the_default_emit_is_one_json_line_on_stdout(capsys) -> None:
    with timing.invocation(cog="evaluator"):
        pass

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert json.loads(out[0])["timing"]["labels"] == {"cog": "evaluator"}


def test_throttling_is_estimated_from_memory_and_left_out_of_idle() -> None:
    """A measured evaluator job at 1,024 MB: 19.5s wall, 8.6s CPU, 2.4s
    waiting on calls. The rest is the CPU quota, not idleness."""
    record = timing._record(
        19.463,
        8.590,
        {"github": (0.619, 1), "api": (1.278, 12), "registry": (0.482, 9)},
        {},
        memory_mb=1024,
    )

    assert record["memory_mb"] == 1024
    assert record["throttled_from"] == "memory"
    # 8.59 × (1769/1024 − 1)
    assert record["throttled_ms"] == pytest.approx(6249, abs=2)
    assert record["unattributed_ms"] == pytest.approx(2245, abs=2)
    assert record["idle_pct"] == pytest.approx(23.8, abs=0.1)


def test_the_estimate_never_eats_into_attributed_waiting() -> None:
    record = timing._record(10.0, 5.0, {"anthropic": (4.5, 1)}, {}, memory_mb=512)

    assert record["throttled_ms"] == 500  # capped: only 0.5s is unaccounted for
    assert record["unattributed_ms"] == 0


def test_a_full_vcpu_is_never_throttled_by_estimate() -> None:
    record = timing._record(3.0, 1.0, {}, {}, memory_mb=3008)

    assert record["throttled_ms"] == 0
    assert record["idle_pct"] == pytest.approx(66.7, abs=0.1)


def test_the_cgroup_counter_is_preferred(tmp_path, monkeypatch, lines) -> None:
    stat = tmp_path / "cpu.stat"
    stat.write_text("usage_usec 10\nthrottled_usec 1000\n")
    monkeypatch.setattr(
        timing, "_CGROUP_THROTTLE", ((str(stat), "throttled_usec", 1e6),)
    )
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024")

    with _run(lines):
        timing._real_sleep(0.05)
        stat.write_text("usage_usec 10\nthrottled_usec 21000\n")  # +20ms

    record = lines[0]["timing"]
    assert record["throttled_from"] == "cgroup"
    assert record["throttled_ms"] == 20
    assert record["memory_mb"] == 1024


def test_off_lambda_there_is_nothing_to_infer(lines) -> None:
    with _run(lines):
        pass

    assert "throttled_ms" not in lines[0]["timing"]
    assert "memory_mb" not in lines[0]["timing"]
