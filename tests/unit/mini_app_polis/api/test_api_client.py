from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.errors import ApiUnavailable, KaianoApiError


def test_post_returns_parsed_json_on_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {"ok": True}

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )
        out = client.post("/v1/ingest", payload={"x": 1})

    mock_client_cls.assert_called_once_with(timeout=10.0)
    mock_http_client.post.assert_called_once_with(
        "https://example.com/v1/ingest",
        json={"x": 1},
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer k_test",
        },
    )
    assert out == {"ok": True}
    response.json.assert_called_once()


def test_post_raises_mini_app_polis_api_error_on_4xx_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.status_code = 404
    response.text = "not found"

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )

        with pytest.raises(KaianoApiError) as excinfo:
            client.post("/v1/missing", payload={"x": 1})

    mock_client_cls.assert_called_once_with(timeout=10.0)
    mock_http_client.post.assert_called_once()

    err = excinfo.value
    assert err.status_code == 404
    assert err.path == "/v1/missing"
    assert "not found" in err.message


def test_post_raises_mini_app_polis_api_error_on_5xx_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.status_code = 500
    response.text = "boom"

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )

        with pytest.raises(KaianoApiError) as excinfo:
            client.post("/v1/fail", payload={"x": 1})

    mock_client_cls.assert_called_once_with(timeout=10.0)
    mock_http_client.post.assert_called_once()

    err = excinfo.value
    assert err.status_code == 500
    assert err.path == "/v1/fail"
    assert "boom" in err.message


def test_post_retries_on_connection_error_and_raises_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_exc = httpx.TransportError("connection reset")

    mock_http_client = MagicMock()
    mock_http_client.post.side_effect = transport_exc

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )

        with pytest.raises(KaianoApiError) as excinfo:
            client.post("/v1/ingest", payload={"x": 1})

    # httpx.Client() is constructed for each attempt inside the loop.
    assert mock_client_cls.call_count == 3
    assert mock_http_client.post.call_count == 3
    mock_client_cls.assert_called()
    mock_http_client.post.assert_called()

    err = excinfo.value
    assert err.status_code == 0
    assert err.path == "/v1/ingest"
    assert "Connection failed after 3 attempts" in err.message


def test_headers_send_the_key_with_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building headers must not talk to anything.

    This used to exchange a machine secret for a Clerk token, so every first
    request carried a round trip before the request it wanted to make. The key
    is the credential now, so nothing happens here but string assembly.
    """
    with patch("httpx.Client") as any_http:
        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )
        h = client._headers()

    any_http.assert_not_called()
    assert h.get("Authorization") == "Bearer k_test"
    assert "X-Owner-Id" not in h


def test_headers_raise_when_no_api_key_is_configured() -> None:
    """A caller with no key fails outright rather than falling back.

    There is no shared credential to fall back to: that fallback is what made
    every cog indistinguishable to the receiving service.
    """
    client = KaianoApiClient(
        base_url="https://example.com",
        timeout=10.0,
        max_retries=3,
    )
    with pytest.raises(KaianoApiError) as excinfo:
        client._headers()
    assert "API key" in excinfo.value.message


def test_post_sends_the_api_key_as_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {"ok": True}

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )
        out = client.post("/v1/ingest", payload={"x": 1})

    mock_http_client.post.assert_called_once_with(
        "https://example.com/v1/ingest",
        json={"x": 1},
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer k_test",
        },
    )
    assert out == {"ok": True}


def test_get_returns_parsed_json_on_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {"items": []}

    mock_http_client = MagicMock()
    mock_http_client.get.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )
        out = client.get("/v1/sets")

    # No `params` kwarg when there are none to send. This assertion used
    # to require `params={}`, which pinned the defect: supplying the kwarg
    # at all makes httpx replace the URL's query string, so any filter
    # written into the path was discarded before the request left.
    mock_http_client.get.assert_called_once_with(
        "https://example.com/v1/sets",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer k_test",
        },
    )
    assert out == {"items": []}


def test_get_passes_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {"n": 1}

    mock_http_client = MagicMock()
    mock_http_client.get.return_value = response

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )
        client.get("/v1/search", params={"q": "test", "limit": "10"})

    mock_http_client.get.assert_called_once_with(
        "https://example.com/v1/search",
        params={"q": "test", "limit": "10"},
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer k_test",
        },
    )


def test_get_retries_on_connection_error_and_raises_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_exc = httpx.TransportError("connection reset")

    mock_http_client = MagicMock()
    mock_http_client.get.side_effect = transport_exc

    with (
        patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            api_key="k_test",
            timeout=10.0,
            max_retries=3,
        )

        with pytest.raises(KaianoApiError) as excinfo:
            client.get("/v1/sets")

    assert mock_client_cls.call_count == 3
    err = excinfo.value
    assert err.status_code == 0
    assert err.path == "/v1/sets"
    assert "Connection failed after 3 attempts" in err.message


def test_kaiano_api_error_importable_from_api_module() -> None:
    from mini_app_polis.api import KaianoApiError

    assert issubclass(KaianoApiError, Exception)


def test_get_preserves_a_query_string_written_into_the_path() -> None:
    """A filter in the path must survive the request.

    httpx replaces a URL's query string whenever `params` is supplied at
    all, and this client used to pass `params=query` unconditionally with
    query defaulting to `{}`. So `/v1/evaluations?repo=x&limit=1` went out
    as `/v1/evaluations`: no filter, default limit, newest row across every
    repo. It returned 200 with plausible-looking data, which is why it went
    unnoticed — it made the evaluator's duplicate check compare one repo's
    finding against another repo's most recent row, and silently drop
    findings that were true.
    """
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": []}

    mock_http_client = MagicMock()
    mock_http_client.get.return_value = response

    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        client.get("/v1/evaluations?repo=deejay-cog&limit=1")

    called_url = mock_http_client.get.call_args[0][0]
    assert called_url.endswith("/v1/evaluations?repo=deejay-cog&limit=1")
    # The kwarg must be absent, not empty: passing it at all is what
    # wiped the query.
    assert "params" not in mock_http_client.get.call_args.kwargs


def test_get_still_sends_explicit_params() -> None:
    """The normal path is unchanged — params are passed when given."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": []}

    mock_http_client = MagicMock()
    mock_http_client.get.return_value = response

    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        client.get("/v1/evaluations", params={"repo": "deejay-cog", "limit": 1})

    assert mock_http_client.get.call_args.kwargs["params"] == {
        "repo": "deejay-cog",
        "limit": 1,
    }


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
#
# The notification path lives on the client so the route and body shape are
# in one place rather than hand-rolled in each cog — the same argument that
# put the key-variable derivation here.


def test_notify_posts_content_to_the_notify_route() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": {"forwarded": True}}

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        out = client.notify("deploy finished")

    assert out == {"data": {"forwarded": True}}
    assert mock_http_client.post.call_args.args[0] == "https://example.com/v1/notify"
    assert mock_http_client.post.call_args.kwargs["json"] == {
        "content": "deploy finished"
    }


def test_notify_sends_embeds_and_username() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response
    embed = {"title": "CI", "color": 1}

    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        client.notify(embeds=[embed], username="deejay-cog")

    body = mock_http_client.post.call_args.kwargs["json"]
    assert body == {"embeds": [embed], "username": "deejay-cog"}
    assert "content" not in body


def test_notify_requires_a_body() -> None:
    """The API rejects an empty message; fail here rather than round-trip it."""
    client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
    with pytest.raises(KaianoApiError):
        client.notify()


def test_notify_raises_on_error_response() -> None:
    """notify is the transport layer — it reports failure by raising.

    pipeline_status is the best-effort layer above it; a caller reaching
    the client directly has said it wants to know.
    """
    response = MagicMock()
    response.status_code = 502
    response.text = "discord rejected it"

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        with pytest.raises(KaianoApiError):
            client.notify("anything")


# ── unavailable vs refused ──────────────────────────────────────────────

_EDGE_404 = (
    '{"status":"error","code":404,"message":"Application not found",'
    '"request_id":"HN0qFBvpTHmbin_5GbGh5g"}'
)
_API_404 = '{"error":{"code":"not_found","message":"Not Found","details":null}}'


def _get_answering(status: int, body: str) -> KaianoApiError:
    response = MagicMock()
    response.status_code = status
    response.text = body
    mock_http_client = MagicMock()
    mock_http_client.get.return_value = response
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        with pytest.raises(KaianoApiError) as excinfo:
            client.get("/v1/deejay/runs")
    return excinfo.value


def test_railways_edge_404_is_unavailable() -> None:
    err = _get_answering(404, _EDGE_404)
    assert isinstance(err, ApiUnavailable)
    assert err.status_code == 404


def test_the_apis_own_404_is_not_unavailable() -> None:
    """A route the API does not have is a bug, not a blip."""
    err = _get_answering(404, _API_404)
    assert not isinstance(err, ApiUnavailable)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_are_unavailable(status: int) -> None:
    assert isinstance(_get_answering(status, "bad gateway"), ApiUnavailable)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 500])
def test_the_apis_refusals_are_not_unavailable(status: int) -> None:
    assert not isinstance(_get_answering(status, _API_404), ApiUnavailable)


def test_exhausted_connection_retries_are_unavailable() -> None:
    mock_http_client = MagicMock()
    mock_http_client.post.side_effect = httpx.TransportError("connection reset")
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        with pytest.raises(ApiUnavailable):
            client.post("/v1/x", payload={})


# ── retrying without piling on ──────────────────────────────────────────


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the client asks for, instead of waiting."""
    waited: list[float] = []
    monkeypatch.setattr("mini_app_polis.api.client._sleep", waited.append)
    return waited


@pytest.fixture(autouse=True)
def _no_real_waits_and_no_cached_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mini_app_polis.api.client._sleep", lambda _s: None)
    monkeypatch.setattr("mini_app_polis.api.client._ETAG_CACHE", {})


def _response(
    status: int,
    body: str = "",
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.content = content if content is not None else body.encode()
    response.headers = httpx.Headers(headers or {})
    response.json.return_value = {"ok": True}
    return response


def _get_with(responses: list[Any], **client_kwargs: Any) -> tuple[Any, MagicMock]:
    mock_http_client = MagicMock()
    mock_http_client.get.side_effect = responses
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(
            base_url="https://example.com", api_key="k_test", **client_kwargs
        )
        try:
            out: Any = client.get("/v1/sets")
        except KaianoApiError as exc:
            out = exc
    return out, mock_http_client


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_a_get_is_retried_after_a_gateway_error_or_429(
    status: int, sleeps: list[float]
) -> None:
    out, http = _get_with([_response(status, "busy"), _response(200)])

    assert out == {"ok": True}
    assert http.get.call_count == 2
    assert len(sleeps) == 1


def test_a_get_waits_as_long_as_retry_after_asks(sleeps: list[float]) -> None:
    out, _ = _get_with([_response(503, "busy", {"Retry-After": "7"}), _response(200)])

    assert out == {"ok": True}
    assert sleeps == [7.0]


def test_no_wait_is_longer_than_the_cap(sleeps: list[float]) -> None:
    _get_with([_response(503, "busy", {"Retry-After": "3600"}), _response(200)])

    assert sleeps == [30.0]


def test_waits_grow_between_attempts(sleeps: list[float]) -> None:
    out, http = _get_with([_response(502, "bad gateway")] * 4, max_retries=4)

    assert isinstance(out, ApiUnavailable)
    assert http.get.call_count == 4
    # Jittered between half and all of 2s, 4s, 8s.
    assert len(sleeps) == 3
    assert 1.0 <= sleeps[0] <= 2.0
    assert 2.0 <= sleeps[1] <= 4.0
    assert 4.0 <= sleeps[2] <= 8.0


def test_the_apis_own_refusal_is_not_retried(sleeps: list[float]) -> None:
    out, http = _get_with([_response(500, _API_404), _response(200)])

    assert isinstance(out, KaianoApiError)
    assert out.status_code == 500
    assert http.get.call_count == 1
    assert sleeps == []


def test_a_lost_connection_is_retried_after_a_wait(sleeps: list[float]) -> None:
    out, http = _get_with([httpx.ReadTimeout("slow"), _response(200)])

    assert out == {"ok": True}
    assert http.get.call_count == 2
    assert len(sleeps) == 1


# ── conditional reads ───────────────────────────────────────────────────


def _export_body(n_entities: int = 0) -> bytes:
    entity = {
        "id": "00000000-0000-0000-0000-000000000001",
        "slug": "sugar-push",
        "canonical_name": "Sugar push",
        "kind": "pattern",
        "overview_md": "",
        "status": "canonical",
        "external_origin": {},
        "aliases": [],
    }
    data = {
        "entities": [entity] * n_entities,
        "instructors": [],
        "sources": [],
        "attributions": [],
        "definitions": [],
        "relations": [],
        "drill_purposes": [],
        "technique_requirements": [],
        "references": [],
        "exported_at": "2026-10-18T12:00:00Z",
    }
    meta = {"count": 1, "total": 1, "version": "test"}
    return json.dumps({"data": data, "meta": meta}).encode()


def _export_with(responses: list[Any]) -> tuple[list[Any], MagicMock]:
    mock_http_client = MagicMock()
    mock_http_client.get.side_effect = responses
    outs: list[Any] = []
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k_test")
        for _ in responses:
            outs.append(client.export_wcs_wiki())
    return outs, mock_http_client


def _if_none_match(call: Any) -> str | None:
    return call.kwargs["headers"].get("If-None-Match")


def test_an_unchanged_export_is_read_from_the_last_copy() -> None:
    body = _export_body(1)
    outs, http = _export_with(
        [
            _response(200, headers={"ETag": '"rev-1"'}, content=body),
            _response(304, headers={"ETag": '"rev-1"'}, content=b""),
        ]
    )

    first, second = http.get.call_args_list
    assert _if_none_match(first) is None
    assert _if_none_match(second) == '"rev-1"'
    assert [len(o.entities) for o in outs] == [1, 1]
    # Each call validates its own model, so one caller's changes never
    # reach the copy the next call reads.
    assert outs[0] is not outs[1]
    assert outs[0].entities[0] is not outs[1].entities[0]


def test_a_changed_export_replaces_the_last_copy() -> None:
    outs, http = _export_with(
        [
            _response(200, headers={"ETag": '"rev-1"'}, content=_export_body(1)),
            _response(200, headers={"ETag": '"rev-2"'}, content=_export_body(2)),
            _response(304, content=b""),
        ]
    )

    assert [_if_none_match(c) for c in http.get.call_args_list] == [
        None,
        '"rev-1"',
        '"rev-2"',
    ]
    assert [len(o.entities) for o in outs] == [1, 2, 2]


def test_an_export_without_an_etag_is_not_kept() -> None:
    _, http = _export_with(
        [
            _response(200, headers={"ETag": '"rev-1"'}, content=_export_body()),
            _response(200, content=_export_body()),
            _response(200, content=_export_body()),
        ]
    )

    assert [_if_none_match(c) for c in http.get.call_args_list] == [
        None,
        '"rev-1"',
        None,
    ]


def test_the_export_gets_a_longer_timeout_than_other_reads() -> None:
    mock_http_client = MagicMock()
    mock_http_client.get.return_value = _response(200, content=_export_body())
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        KaianoApiClient(base_url="https://example.com", api_key="k").export_wcs_wiki()

    mock_client_cls.assert_called_once_with(timeout=120.0)


def test_a_304_with_no_copy_is_an_error() -> None:
    with pytest.raises(KaianoApiError) as excinfo:
        _export_with([_response(304, content=b"")])
    assert excinfo.value.status_code == 304


def test_a_wait_that_would_cross_the_run_deadline_is_skipped(
    sleeps: list[float],
) -> None:
    from mini_app_polis.deadline import run_deadline

    with run_deadline(5):
        out, http = _get_with(
            [_response(503, "busy", {"Retry-After": "10"}), _response(200)]
        )

    assert isinstance(out, ApiUnavailable)
    assert out.status_code == 503
    assert http.get.call_count == 1
    assert sleeps == []


def test_no_read_starts_past_the_run_deadline() -> None:
    from mini_app_polis.deadline import RunDeadlineReached, run_deadline

    mock_http_client = MagicMock()
    with patch("mini_app_polis.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client
        client = KaianoApiClient(base_url="https://example.com", api_key="k")
        with run_deadline(0), pytest.raises(RunDeadlineReached):
            client.get("/v1/sets")

    mock_http_client.get.assert_not_called()
