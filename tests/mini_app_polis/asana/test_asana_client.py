"""Tests for the Asana client — create, external-id lookup, tags, errors."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from mini_app_polis.asana import (
    AsanaAPIError,
    AsanaAuthError,
    AsanaClient,
    AsanaTaskInput,
)

_TOKEN = "pat-test"
_WORKSPACE = "ws-1"


def _client(handler) -> AsanaClient:
    """Build a client whose HTTP calls are served by ``handler``."""
    return AsanaClient(
        access_token=_TOKEN,
        workspace_gid=_WORKSPACE,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _task(**overrides) -> AsanaTaskInput:
    base = {
        "name": "Send the report",
        "html_notes": "<body>because it is due</body>",
        "project_gid": "proj-1",
    }
    base.update(overrides)
    return AsanaTaskInput(**base)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_posts_minimal_payload_and_returns_gid() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = httpx.Request("POST", "http://x", content=request.content)
        import json

        seen["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "task-1"}})

    gid = _client(handler).create_task(_task())

    assert gid == "task-1"
    assert seen["url"] == "https://app.asana.com/api/1.0/tasks"
    assert seen["auth"] == f"Bearer {_TOKEN}"
    # No section given, so the task is attached with `projects`.
    assert seen["data"]["projects"] == ["proj-1"]
    assert "memberships" not in seen["data"]
    # Optional fields are omitted entirely rather than sent as null.
    for absent in ("assignee", "due_on", "tags", "external"):
        assert absent not in seen["data"]


def test_create_task_uses_memberships_when_a_section_is_given() -> None:
    """Section placement rides on the create call, not a follow-up move."""
    calls: list[str] = []
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(str(request.url))
        captured["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "task-2"}})

    _client(handler).create_task(_task(section_gid="sect-9"))

    assert len(calls) == 1
    assert captured["data"]["memberships"] == [
        {"project": "proj-1", "section": "sect-9"}
    ]
    assert "projects" not in captured["data"]


def test_create_task_sends_all_optional_fields_when_present() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "task-3"}})

    _client(handler).create_task(
        _task(
            assignee="me",
            due_on=date(2026, 9, 9),
            tag_gids=("tag-a", "tag-b"),
            external_id="voicenote.abc123",
        )
    )

    assert captured["data"]["assignee"] == "me"
    assert captured["data"]["due_on"] == "2026-09-09"
    assert captured["data"]["tags"] == ["tag-a", "tag-b"]
    assert captured["data"]["external"] == {"gid": "voicenote.abc123"}


def test_create_task_without_gid_in_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": {"name": "no gid here"}})

    with pytest.raises(AsanaAPIError):
        _client(handler).create_task(_task())


# ---------------------------------------------------------------------------
# find_task_by_external_id
# ---------------------------------------------------------------------------


def test_find_task_by_external_id_returns_gid() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"gid": "task-7"}})

    got = _client(handler).find_task_by_external_id("voicenote.abc123")

    assert got == "task-7"
    assert seen["path"] == "/api/1.0/tasks/external:voicenote.abc123"


def test_find_task_by_external_id_percent_encodes_the_id() -> None:
    """An id with a colon must not be read as part of Asana's own prefix."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = str(request.url)
        return httpx.Response(404, json={"errors": [{"message": "Not Found"}]})

    assert _client(handler).find_task_by_external_id("a:b/c") is None
    assert "a%3Ab%2Fc" in seen["raw"]


def test_find_task_by_external_id_returns_none_on_404() -> None:
    """404 is 'no such task', which is an answer, not a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"message": "Not Found"}]})

    assert _client(handler).find_task_by_external_id("nope") is None


def test_find_task_by_external_id_still_raises_on_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(AsanaAPIError):
        _client(handler).find_task_by_external_id("x")


# ---------------------------------------------------------------------------
# find_or_create_tag
# ---------------------------------------------------------------------------


def test_find_or_create_tag_returns_existing_match_case_insensitively() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"data": [{"gid": "tag-1", "name": "Review"}], "next_page": None},
        )

    assert _client(handler).find_or_create_tag("review") == "tag-1"


def test_find_or_create_tag_follows_pagination_before_creating() -> None:
    pages = [
        {
            "data": [{"gid": "tag-1", "name": "alpha"}],
            "next_page": {"offset": "page2"},
        },
        {"data": [{"gid": "tag-2", "name": "beta"}], "next_page": None},
    ]
    seen_offsets: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_offsets.append(request.url.params.get("offset"))
        return httpx.Response(200, json=pages[len(seen_offsets) - 1])

    assert _client(handler).find_or_create_tag("beta") == "tag-2"
    assert seen_offsets == [None, "page2"]


def test_find_or_create_tag_creates_when_absent() -> None:
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "next_page": None})
        posted["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "tag-new"}})

    assert _client(handler).find_or_create_tag(" follow-up ") == "tag-new"
    assert posted["data"] == {"name": "follow-up", "workspace": _WORKSPACE}


def test_find_or_create_tag_caches_across_calls() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            json={"data": [{"gid": "tag-1", "name": "review"}], "next_page": None},
        )

    client = _client(handler)
    assert client.find_or_create_tag("review") == "tag-1"
    assert client.find_or_create_tag("REVIEW") == "tag-1"
    assert calls == ["GET"]


def test_find_or_create_tag_without_workspace_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not reach the network")

    client = AsanaClient(
        access_token=_TOKEN,
        workspace_gid=None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AsanaAPIError, match="ASANA_WORKSPACE_ID"):
        client.find_or_create_tag("review")


# ---------------------------------------------------------------------------
# Auth and error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_raise_asana_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"message": "Not Authorized"}]})

    with pytest.raises(AsanaAuthError, match="ASANA_ACCESS_TOKEN"):
        _client(handler).create_task(_task())


def test_server_error_surfaces_asanas_own_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"message": "Server Error"}]})

    with pytest.raises(AsanaAPIError, match="Server Error"):
        _client(handler).create_task(_task())


def test_missing_token_raises_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not reach the network")

    client = AsanaClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AsanaAuthError, match="ASANA_ACCESS_TOKEN"):
        client.create_task(_task())


def test_token_is_read_per_request_so_rotation_takes_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating the secret must not require a process restart."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(201, json={"data": {"gid": "t"}})

    client = AsanaClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "old")
    client.create_task(_task())
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "new")
    client.create_task(_task())

    assert seen == ["Bearer old", "Bearer new"]
