"""``file-task``: put a task on the board from a working session, by hand.

The task goes through ``POST /v1/tasks`` like every other source, so the task
manager stays the one writer to the board and mints the task's id. The caller
never sends one.

Run it under Doppler in the ``owner-session`` project, which holds
``CLAUDE_SESSION_API_KEY``::

    doppler run --project owner-session --config prd -- \\
        file-task --key split-the-vault --title "Split the Doppler vault" \\
        --where infra --effort M --executor cowork --body "Why: …"

or with the whole draft as JSON (``--json draft.json``, or ``--json -`` for
standard input), in the shape of ``mini_app_polis.api.contract.TaskDraft``.

``--key`` is required and becomes ``manual.<key>``. Pick something stable
that names the intent (a short slug): asking again with the same key files
nothing new, which is what makes a retry safe. A key derived from the draft's
text would change whenever a retry reworded it.

It prints the key and the API's answer, never the API key. A missing key, a
draft the contract refuses or a refused request exits non-zero with a
one-line reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from .client import KaianoApiClient
from .contract import TaskCreateRequest, TaskDraft
from .contract.groomings import GroomingExecutor
from .errors import KaianoApiError

#: The machine a working session files tasks as.
MACHINE_NAME = "claude-session"

#: The executor a task has when ``--executor`` is not given.
DEFAULT_EXECUTOR = "agent"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-task",
        description="File one task on the board through POST /v1/tasks.",
    )
    parser.add_argument(
        "--key",
        required=True,
        help="A stable key for the work (a short slug); becomes manual.<key>.",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="The draft as JSON (a TaskDraft); '-' reads standard input.",
    )
    parser.add_argument("--title", default="")
    parser.add_argument("--done-when")
    parser.add_argument("--where")
    parser.add_argument("--constraints")
    parser.add_argument("--effort", choices=["S", "M", "L"])
    parser.add_argument("--body")
    parser.add_argument(
        "--executor",
        choices=[DEFAULT_EXECUTOR, *get_args(GroomingExecutor)],
        default=None,
        help="Who works the task (default: agent).",
    )
    return parser


def _draft(args: argparse.Namespace) -> TaskDraft:
    """The draft from ``--json``, or from the flags, with the executor as a
    label (the task manager writes it into the task's Executor line)."""
    if args.json:
        text = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        draft = TaskDraft.model_validate_json(text)
    else:
        draft = TaskDraft(
            title=args.title,
            done_when=args.done_when,
            where=args.where,
            constraints=args.constraints,
            effort=args.effort,
            body=args.body,
        )
    executor = args.executor
    if executor and executor != DEFAULT_EXECUTOR:
        labels = [label for label in draft.labels if label.lower() != executor]
        draft = draft.model_copy(update={"labels": [*labels, executor]})
    return draft


def main(
    argv: Sequence[str] | None = None,
    *,
    client: KaianoApiClient | None = None,
) -> int:
    """Run the command. Returns the exit code."""
    args = _parser().parse_args(argv)
    try:
        request = TaskCreateRequest(
            source="manual", external_id=f"manual.{args.key}", draft=_draft(args)
        )
    except (ValidationError, ValueError, OSError) as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"file-task: the draft was refused: {first}", file=sys.stderr)
        return 2

    api = client or KaianoApiClient.from_env(MACHINE_NAME)
    try:
        accepted = api.request_task(request)
    except KaianoApiError as exc:
        print(
            f"file-task: {request.external_id} not filed: "
            f"{exc.status_code} {exc.message.splitlines()[0] if exc.message else ''}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "external_id": accepted.external_id,
                "accepted": accepted.accepted,
                "message_id": accepted.message_id,
            }
        )
    )
    return 0


def cli() -> None:
    """The ``file-task`` console script."""
    sys.exit(main())
