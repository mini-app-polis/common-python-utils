from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from kaiano.api import KaianoApiClient
from kaiano.api.errors import KaianoApiError


def test_post_returns_parsed_json_on_200_response() -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {"ok": True}

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with patch("kaiano.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            owner_id="owner-123",
            timeout=10.0,
            max_retries=3,
        )
        out = client.post("/v1/ingest", payload={"x": 1})

    mock_client_cls.assert_called_once_with(timeout=10.0)
    mock_http_client.post.assert_called_once_with(
        "https://example.com/v1/ingest",
        json={"x": 1},
        headers={"Content-Type": "application/json", "X-Owner-Id": "owner-123"},
    )
    assert out == {"ok": True}
    response.json.assert_called_once()


def test_post_raises_kaiano_api_error_on_4xx_response() -> None:
    response = MagicMock()
    response.status_code = 404
    response.text = "not found"

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with patch("kaiano.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            owner_id="owner-123",
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


def test_post_raises_kaiano_api_error_on_5xx_response() -> None:
    response = MagicMock()
    response.status_code = 500
    response.text = "boom"

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = response

    with patch("kaiano.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            owner_id="owner-123",
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


def test_post_retries_on_connection_error_and_raises_after_max_retries() -> None:
    transport_exc = httpx.TransportError("connection reset")

    mock_http_client = MagicMock()
    mock_http_client.post.side_effect = transport_exc

    with patch("kaiano.api.client.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = KaianoApiClient(
            base_url="https://example.com",
            owner_id="owner-123",
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


def test_from_env_uses_environment_variables(monkeypatch) -> None:
    monkeypatch.delenv("KAIANO_API_BASE_URL", raising=False)
    monkeypatch.delenv("KAIANO_API_OWNER_ID", raising=False)
    monkeypatch.delenv("OWNER_ID", raising=False)

    from_env = KaianoApiClient.from_env()
    assert from_env.base_url == ""

    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://internal.example.com/")
    monkeypatch.setenv("OWNER_ID", "env-owner")
    monkeypatch.delenv("KAIANO_API_OWNER_ID", raising=False)

    client = KaianoApiClient.from_env()
    assert client.base_url == "https://internal.example.com"
    assert client.owner_id == "env-owner"


def test_headers_include_content_type_and_owner_id() -> None:
    client = KaianoApiClient(
        base_url="https://example.com",
        owner_id="owner-123",
        timeout=10.0,
        max_retries=3,
    )
    assert client._headers() == {
        "Content-Type": "application/json",
        "X-Owner-Id": "owner-123",
    }
