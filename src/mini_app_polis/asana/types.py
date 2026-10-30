"""Input shapes for :mod:`mini_app_polis.asana`.

``AsanaTaskInput`` is deliberately source-agnostic. It carries no
notion of where the task came from — a voice note, a Discord message,
a conformance finding — because the point of putting this in the
shared library is that each of those callers composes the same shape.
The only hook a source gets is ``external_id``, an opaque string the
caller namespaces however it likes (``voicenote.<drive_file_id>``,
``discord.<message_id>``).

``AsanaSection``, ``AsanaTaskRecord`` and ``AsanaStory`` are what the read
methods return: a board's columns, its tasks as a caller that grooms them
needs to see them, and a task's history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class AsanaTaskInput:
    """Everything needed to create one Asana task.

    Field-name note: Asana calls the task title ``name`` and the body
    ``html_notes``; unlike Todoist's ``content``/``description`` pair
    those names are unambiguous, so they are used verbatim here rather
    than renamed at the client boundary.
    """

    name: str
    """Task title. Asana rejects an empty string — callers must
    substitute a placeholder rather than send one."""

    html_notes: str
    """Task body as Asana rich text, wrapped in ``<body>``. Build it
    with :func:`mini_app_polis.asana.rich_text.rich_text_body` so
    caller-supplied text is escaped; Asana returns 400 on malformed
    markup, and transcribed or model-authored prose contains ``<``
    and ``&`` often enough to matter."""

    project_gid: str
    """Project the task is created in."""

    section_gid: str | None = None
    """Section (board column) within ``project_gid``. When ``None`` the
    task lands in the project's default first section, which is
    whatever happens to be leftmost — pass this explicitly if the
    column matters."""

    assignee: str | None = None
    """Assignee gid, an email, or the literal ``"me"``. ``None``
    leaves the task unassigned."""

    due_on: date | None = None
    """Due date. Sent as ``YYYY-MM-DD``; Asana renders it in the
    account's timezone."""

    tag_gids: tuple[str, ...] = ()
    """Tags to apply at creation. Asana tags are workspace objects, not
    free strings — resolve names to gids with
    :meth:`~mini_app_polis.asana.client.AsanaClient.find_or_create_tag`
    first."""

    external_id: str | None = None
    """Caller-owned idempotency key, stored on the task's ``external``
    field and retrievable with
    :meth:`~mini_app_polis.asana.client.AsanaClient.find_task_by_external_id`.

    This is the reason to prefer Asana over a task manager without
    search: the caller does not have to embed a marker in the body and
    scan for it, and so does not miss tasks that have been
    completed and dropped out of the active list."""


@dataclass(frozen=True)
class AsanaSection:
    """One column of a board."""

    gid: str
    name: str


@dataclass(frozen=True)
class AsanaTaskRecord:
    """One task as a project listing returns it."""

    gid: str
    name: str
    notes: str
    """The body as plain text. Asana derives it from ``html_notes``; it is
    what a reader of the task's fields wants, without the markup."""

    completed: bool
    section_gid: str | None
    """The task's column in the project it was listed from, or None when
    Asana reports no section for that project."""

    section_name: str | None
    tag_names: tuple[str, ...] = ()
    assignee_gid: str | None = None
    """The assignee's user gid, or None when the task is unassigned."""

    html_notes: str = ""
    """The body as Asana rich text, wrapped in ``<body>``. A caller that
    rewrites part of a body edits this rather than ``notes``, so the
    task's formatting survives."""

    external_id: str | None = None
    """The caller-owned key the task was created with
    (:attr:`AsanaTaskInput.external_id`), or None when it has none."""

    created_at: datetime | None = None
    """When the task was created, timezone-aware (UTC)."""


@dataclass(frozen=True)
class AsanaStory:
    """One entry in a task's history: a comment, a column change, or
    anything else Asana records."""

    gid: str
    kind: str
    """Asana's ``resource_subtype`` as it is: ``comment_added`` for a
    comment, ``section_changed`` for a move between columns, and so on."""

    created_at: datetime | None = None
    """When it happened, timezone-aware (UTC)."""

    text: str | None = None
    """A comment's text. None for every other kind."""

    section_gid: str | None = None
    """For ``section_changed``, the column the task moved to."""

    section_name: str | None = None
