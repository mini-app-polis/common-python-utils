"""HTTP client for the Asana API.

**Auth:** a single long-lived personal access token sent as
``Authorization: Bearer <token>``. No OAuth, no refresh — the token is
valid until it is revoked in the Asana UI (My Settings → Apps →
Manage Developer Apps → Personal Access Tokens).

**Scope:** this client covers only what the ecosystem creates tasks
with today. Three methods, no more:

  - ``create_task`` — POST /tasks
  - ``find_task_by_external_id`` — GET /tasks/external:<id>
  - ``find_or_create_tag`` — GET /tags + POST /tags

Reading, updating and commenting on tasks are deliberately absent.
The Discord-sourced task-update path that would need them is not
built, and a method with no caller is a method nothing keeps honest.

**Idempotency:** Asana stores an app-scoped ``external`` object on
every task, and a task can be addressed by it directly. That is the
whole reason a caller can ask "did I already create this?" in one
request. The Todoist client this replaces had no such lookup: it
listed the project and substring-scanned descriptions for an embedded
marker, which silently missed any task the operator had already
completed, since Todoist's list endpoint returns active tasks only.

**Env vars:**
  ASANA_ACCESS_TOKEN   — personal access token
  ASANA_WORKSPACE_ID   — workspace gid; needed only for tag resolution

Reference: https://developers.asana.com/reference/tasks
"""

from __future__ import annotations

import logging as _logging
import os
from typing import Any
from urllib.parse import quote

import httpx

from .errors import AsanaAPIError, AsanaAuthError
from .types import AsanaTaskInput

_log = _logging.getLogger(__name__)

#: Asana's only API version. Unlike Todoist there is no deprecated
#: predecessor to guard against.
ASANA_API_BASE = "https://app.asana.com/api/1.0"

#: Asana caps list endpoints at 100 records per page.
_PAGE_LIMIT = 100


class AsanaClient:
    """Thin Asana API wrapper.

    The access token is resolved on every request rather than captured
    at construction, so rotating ``ASANA_ACCESS_TOKEN`` in Doppler
    takes effect on the next call without restarting the process.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        workspace_gid: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._access_token = access_token
        self._workspace_gid = workspace_gid
        self._http: httpx.Client = http_client or httpx.Client(timeout=timeout)
        # name (lowercased) -> tag gid. Tags are stable workspace
        # objects, so a process-lifetime cache cannot go stale in any
        # way that matters; the worst case is a tag renamed in the UI
        # mid-run, which still resolves to the right object.
        self._tag_cache: dict[str, str] = {}

    @classmethod
    def from_env(cls, **kwargs: Any) -> AsanaClient:
        """Build a client from environment variables.

        Mirrors ``KaianoApiClient.from_env`` so consumers reach for the
        same constructor across the shared library.
        """
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def _token(self) -> str:
        token = (
            self._access_token or os.environ.get("ASANA_ACCESS_TOKEN") or ""
        ).strip()
        if not token:
            raise AsanaAuthError(
                "No Asana access token. Set ASANA_ACCESS_TOKEN in Doppler, or "
                "pass access_token= explicitly. Mint one at "
                "https://app.asana.com/0/my-apps."
            )
        return token

    def _workspace(self) -> str:
        workspace = (
            self._workspace_gid or os.environ.get("ASANA_WORKSPACE_ID") or ""
        ).strip()
        if not workspace:
            raise AsanaAPIError(
                None,
                "No Asana workspace gid. Set ASANA_WORKSPACE_ID in Doppler, or "
                "pass workspace_gid= explicitly. It is required to resolve tag "
                "names, which are workspace-scoped objects.",
                "/tags",
            )
        return workspace

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_task(self, task: AsanaTaskInput) -> str:
        """Create a task. Returns the new task's gid.

        Section placement rides on the create call via ``memberships``
        rather than a follow-up ``POST /sections/{gid}/addTask``. One
        request means the task cannot exist in the wrong column: a
        two-step version that failed on the second step would leave a
        task whose ``external_id`` makes every retry return early
        without ever moving it.
        """
        data: dict[str, Any] = {
            "name": task.name,
            "html_notes": task.html_notes,
        }
        if task.section_gid:
            data["memberships"] = [
                {"project": task.project_gid, "section": task.section_gid}
            ]
        else:
            data["projects"] = [task.project_gid]
        if task.assignee:
            data["assignee"] = task.assignee
        if task.due_on is not None:
            data["due_on"] = task.due_on.isoformat()
        if task.tag_gids:
            data["tags"] = list(task.tag_gids)
        if task.external_id:
            data["external"] = {"gid": task.external_id}

        body = self._request("POST", "/tasks", json_payload={"data": data})
        gid = body.get("gid") if isinstance(body, dict) else None
        if not isinstance(gid, str):
            raise AsanaAPIError(
                None, f"create_task response had no gid: {body!r}", "/tasks"
            )
        return gid

    def find_task_by_external_id(self, external_id: str) -> str | None:
        """Return the gid of the task carrying ``external_id``, or None.

        Asana scopes ``external`` data to the app that wrote it, so this
        only ever finds tasks this token created. That is the desired
        behaviour for an idempotency check.

        Unlike a list-and-scan, this sees completed tasks too — a note
        already triaged and checked off will not be recreated.
        """
        path = f"/tasks/external:{quote(external_id, safe='')}"
        body = self._request("GET", path, params={"opt_fields": "gid"}, allow_404=True)
        if body is None:
            return None
        gid = body.get("gid") if isinstance(body, dict) else None
        return gid if isinstance(gid, str) else None

    def find_or_create_tag(self, name: str) -> str:
        """Resolve a tag name to its gid, creating the tag if needed.

        Asana tags are workspace objects with gids, not the free
        strings Todoist labels were, so a caller that wants to apply
        "review" has to look it up first. Results are cached for the
        life of the client.
        """
        key = name.strip().lower()
        if not key:
            raise AsanaAPIError(None, "Tag name cannot be empty.", "/tags")
        cached = self._tag_cache.get(key)
        if cached is not None:
            return cached

        workspace = self._workspace()
        offset: str | None = None
        while True:
            params: dict[str, Any] = {
                "workspace": workspace,
                "opt_fields": "name",
                "limit": _PAGE_LIMIT,
            }
            if offset:
                params["offset"] = offset
            page = self._request_envelope("GET", "/tags", params=params)
            for tag in page.get("data") or []:
                if not isinstance(tag, dict):
                    continue
                tag_name = tag.get("name")
                tag_gid = tag.get("gid")
                if isinstance(tag_name, str) and isinstance(tag_gid, str):
                    self._tag_cache.setdefault(tag_name.strip().lower(), tag_gid)
            found = self._tag_cache.get(key)
            if found is not None:
                return found
            next_page = page.get("next_page")
            offset = next_page.get("offset") if isinstance(next_page, dict) else None
            if not offset:
                break

        created = self._request(
            "POST",
            "/tags",
            json_payload={"data": {"name": name.strip(), "workspace": workspace}},
        )
        gid = created.get("gid") if isinstance(created, dict) else None
        if not isinstance(gid, str):
            raise AsanaAPIError(
                None, f"create tag response had no gid: {created!r}", "/tags"
            )
        self._tag_cache[key] = gid
        return gid

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        """Issue a request and return the unwrapped ``data`` member.

        Returns ``None`` when ``allow_404`` is set and Asana answers
        404 — the one case where "not there" is an answer rather than
        a failure.
        """
        envelope = self._request_envelope(
            method,
            path,
            json_payload=json_payload,
            params=params,
            allow_404=allow_404,
        )
        if envelope is None:
            return None
        if "data" not in envelope:
            raise AsanaAPIError(
                None, f"response had no data member: {envelope!r}", path
            )
        return envelope["data"]

    def _request_envelope(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        """Issue a request and return the full response envelope.

        Kept separate from :meth:`_request` because paginated endpoints
        need ``next_page``, which sits beside ``data`` rather than
        inside it.
        """
        url = f"{ASANA_API_BASE}{path}"
        try:
            response = self._http.request(
                method,
                url,
                headers=self._headers(),
                json=json_payload,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise AsanaAPIError(None, f"request failed: {exc}", path) from exc

        if allow_404 and response.status_code == 404:
            return None
        self._raise_for_status(response, path)

        try:
            return response.json()
        except ValueError as exc:
            raise AsanaAPIError(
                response.status_code,
                f"response was not JSON: {response.text[:200]}",
                path,
            ) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = _error_detail(response)
        if response.status_code in (401, 403):
            _log.error(
                "asana auth failure %s on %s: %s", response.status_code, path, detail
            )
            raise AsanaAuthError(
                f"Asana auth failure {response.status_code} on {path}: {detail}. "
                "Mint a new personal access token at "
                "https://app.asana.com/0/my-apps and rotate ASANA_ACCESS_TOKEN "
                "in Doppler."
            )
        raise AsanaAPIError(response.status_code, detail, path)


def _error_detail(response: httpx.Response) -> str:
    """Pull the human-readable message out of Asana's error envelope.

    Asana answers failures with ``{"errors": [{"message": "...", ...}]}``.
    Falls back to the raw body when the shape is not what is documented,
    which is exactly the case where the raw body is worth seeing.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            messages = [
                str(e.get("message"))
                for e in errors
                if isinstance(e, dict) and e.get("message")
            ]
            if messages:
                return "; ".join(messages)
    return response.text[:200]
