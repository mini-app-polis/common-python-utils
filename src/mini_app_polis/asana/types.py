"""Input shapes for :mod:`mini_app_polis.asana`.

``AsanaTaskInput`` is deliberately source-agnostic. It carries no
notion of where the task came from — a voice note, a Discord message,
a conformance finding — because the point of putting this in the
shared library is that each of those callers composes the same shape.
The only hook a source gets is ``external_id``, an opaque string the
caller namespaces however it likes (``voicenote.<drive_file_id>``,
``discord.<message_id>``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
    search: the caller no longer has to embed a marker in the body and
    scan for it, and so no longer misses tasks that have been
    completed and dropped out of the active list."""
