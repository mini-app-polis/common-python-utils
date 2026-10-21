"""Tests for the Asana client — create, external-id lookup, tags, errors."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from mini_app_polis.asana import (
    AsanaAPIError,
    AsanaAuthError,
    AsanaClient,
    AsanaSection,
    AsanaTaskInput,
    AsanaTaskRecord,
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


def test_create_task_with_a_section_still_sends_a_container() -> None:
    """Regression: memberships alone is a 400.

    Asana requires one of workspace, parent or projects on create;
    memberships is not on that list, so a section-placed task needs the
    workspace as its container.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "task-2"}})

    _client(handler).create_task(_task(section_gid="sect-9"))

    assert captured["data"]["workspace"] == _WORKSPACE


def test_create_task_with_a_section_but_no_workspace_raises_before_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail with a message naming the missing var, not Asana's 400."""
    monkeypatch.delenv("ASANA_WORKSPACE_ID", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not reach the network")

    client = AsanaClient(
        access_token=_TOKEN,
        workspace_gid=None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AsanaAPIError, match="ASANA_WORKSPACE_ID"):
        client.create_task(_task(section_gid="sect-9"))


def test_create_task_without_a_section_needs_no_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``projects`` is itself a valid container, so workspace is optional."""
    monkeypatch.delenv("ASANA_WORKSPACE_ID", raising=False)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["data"] = json.loads(request.content)["data"]
        return httpx.Response(201, json={"data": {"gid": "task-2"}})

    client = AsanaClient(
        access_token=_TOKEN,
        workspace_gid=None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.create_task(_task())

    assert captured["data"]["projects"] == ["proj-1"]
    assert "workspace" not in captured["data"]


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
# list_task_names
# ---------------------------------------------------------------------------


def test_list_task_names_pages_through_the_project() -> None:
    pages = [
        {
            "data": [{"gid": "t1", "name": "VN-1 · one"}, {"gid": "t2", "name": "two"}],
            "next_page": {"offset": "page2"},
        },
        {"data": [{"gid": "t3", "name": "VN-2 · three"}], "next_page": None},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    names = _client(handler).list_task_names("proj-1")

    assert names == ["VN-1 · one", "two", "VN-2 · three"]
    assert seen[0].url.path == "/api/1.0/projects/proj-1/tasks"
    assert seen[0].url.params.get("opt_fields") == "name"
    assert seen[0].url.params.get("offset") is None
    assert seen[1].url.params.get("offset") == "page2"


def test_list_task_names_includes_completed_tasks() -> None:
    """No completed_since filter, so a completed task's id stays taken."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "completed_since" not in request.url.params
        return httpx.Response(200, json={"data": [], "next_page": None})

    assert _client(handler).list_task_names("proj-1") == []


def test_list_task_names_skips_records_without_a_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"gid": "t1"}, "junk", {"gid": "t2", "name": "kept"}]},
        )

    assert _client(handler).list_task_names("proj-1") == ["kept"]


# ---------------------------------------------------------------------------
# list_sections, list_project_tasks, move_task_to_section
# ---------------------------------------------------------------------------


def test_list_sections_returns_columns_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1.0/projects/proj-1/sections"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"gid": "s1", "name": "Ideas"},
                    {"gid": "s2", "name": "Ready"},
                    {"gid": "s3"},
                ]
            },
        )

    assert _client(handler).list_sections("proj-1") == [
        AsanaSection(gid="s1", name="Ideas"),
        AsanaSection(gid="s2", name="Ready"),
    ]


def test_list_project_tasks_reads_body_column_and_tags_across_pages() -> None:
    pages = [
        {
            "data": [
                {
                    "gid": "t1",
                    "name": "MINI-1 · one",
                    "notes": "Done when: it works",
                    "completed": False,
                    "tags": [{"gid": "g1", "name": "review"}],
                    "memberships": [
                        {
                            "project": {"gid": "other"},
                            "section": {"gid": "x", "name": "Elsewhere"},
                        },
                        {
                            "project": {"gid": "proj-1"},
                            "section": {"gid": "s1", "name": "Ideas"},
                        },
                    ],
                }
            ],
            "next_page": {"offset": "page2"},
        },
        {
            "data": [
                {"gid": "t2", "name": "two", "completed": True, "memberships": []},
                {"gid": "t3"},
            ],
            "next_page": None,
        },
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    tasks = _client(handler).list_project_tasks("proj-1")

    assert tasks == [
        AsanaTaskRecord(
            gid="t1",
            name="MINI-1 · one",
            notes="Done when: it works",
            completed=False,
            section_gid="s1",
            section_name="Ideas",
            tag_names=("review",),
        ),
        AsanaTaskRecord(
            gid="t2",
            name="two",
            notes="",
            completed=True,
            section_gid=None,
            section_name=None,
        ),
    ]
    assert "notes" in seen[0].url.params["opt_fields"]
    assert "completed_since" not in seen[0].url.params
    assert seen[1].url.params.get("offset") == "page2"


def test_move_task_to_section_posts_the_task() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {}})

    _client(handler).move_task_to_section("t1", "s2")

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/1.0/sections/s2/addTask"
    assert json.loads(seen[0].content) == {"data": {"task": "t1"}}


# ---------------------------------------------------------------------------
# get_task, update_task, remove_tag, add_comment, list_comments
# ---------------------------------------------------------------------------


def test_get_task_reads_one_task_as_the_listing_does() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1.0/tasks/t1"
        assert "memberships.section.name" in request.url.params["opt_fields"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "t1",
                    "name": "MINI-1 · one",
                    "notes": "body",
                    "completed": False,
                    "tags": [{"name": "review"}],
                    "memberships": [
                        {
                            "project": {"gid": "proj-1"},
                            "section": {"gid": "s2", "name": "Refining"},
                        }
                    ],
                }
            },
        )

    task = _client(handler).get_task("t1", "proj-1")
    assert task == AsanaTaskRecord(
        gid="t1",
        name="MINI-1 · one",
        notes="body",
        completed=False,
        section_gid="s2",
        section_name="Refining",
        tag_names=("review",),
    )


def test_get_task_of_a_deleted_task_is_none() -> None:
    assert _client(lambda _request: httpx.Response(404)).get_task("t1", "p") is None


def test_update_task_sends_only_what_changes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {}})

    client = _client(handler)
    client.update_task("t1", html_notes="<body>x</body>")
    client.update_task("t1")
    client.update_task("t1", name="MINI-2 · two", html_notes="<body>y</body>")

    assert len(seen) == 2
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/1.0/tasks/t1"
    assert json.loads(seen[0].content) == {"data": {"html_notes": "<body>x</body>"}}
    assert json.loads(seen[1].content) == {
        "data": {"name": "MINI-2 · two", "html_notes": "<body>y</body>"}
    }


def test_remove_tag_resolves_the_name_then_removes_it() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/1.0/tags":
            return httpx.Response(200, json={"data": [{"gid": "g9", "name": "Review"}]})
        return httpx.Response(200, json={"data": {}})

    _client(handler).remove_tag("t1", "review")
    assert seen[-1].url.path == "/api/1.0/tasks/t1/removeTag"
    assert json.loads(seen[-1].content) == {"data": {"tag": "g9"}}


def test_comments_are_posted_and_read_back_without_the_history() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {}})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"resource_subtype": "section_changed", "text": "moved"},
                    {"resource_subtype": "comment_added", "text": "first"},
                    {"resource_subtype": "comment_added", "text": "second"},
                ]
            },
        )

    client = _client(handler)
    client.add_comment("t1", "Grooming questions: why?")
    assert json.loads(seen[0].content) == {"data": {"text": "Grooming questions: why?"}}
    assert seen[0].url.path == "/api/1.0/tasks/t1/stories"
    assert client.list_comments("t1") == ["first", "second"]


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
