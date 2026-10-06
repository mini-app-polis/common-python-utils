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
