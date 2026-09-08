from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.errors import KaianoApiError


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
