"""``request-sweep``: ask for a sweep of the board now, by hand.

The sweep goes through ``POST /v1/tasks/sweeps``, which puts a ``task.sweep``
on task-manager-cog's queue, so it runs in line with every other job there.

Without flags it is the sweep the hourly schedule sends: it
refines and dispatches agents only if the time of day is one set for them.
``--refine`` makes it the sweep for ``REFINE_TIMES`` and ``--agents`` the one
for ``AGENT_TIMES``, whatever the clock says. Neither turns on what is off
(``REFINE_DISPATCH``, ``AGENT_DISPATCH``, a dry run) or raises a cap.
``--groom`` is the old name of ``--refine`` and still works.

Run it under Doppler in the ``owner-session`` project, which holds
``CLAUDE_SESSION_API_KEY``::

    doppler run --project owner-session --config prd -- \\
        request-sweep --refine --agents

It prints the API's answer, never the API key. A refused request exits
non-zero with a one-line reason. Asking twice runs two sweeps.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .client import KaianoApiClient
from .contract import TaskSweepRequest
from .errors import KaianoApiError

#: The machine a working session asks for a sweep as.
MACHINE_NAME = "claude-session"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="request-sweep",
        description="Ask for a sweep of the board now, through POST /v1/tasks/sweeps.",
    )
    parser.add_argument(
        "--refine",
        "--groom",
        dest="refine",
        action="store_true",
        help=(
            "Send tasks for refining in this sweep, whatever the time of day. "
            "--groom is its old name."
        ),
    )
    parser.add_argument(
        "--agents",
        action="store_true",
        help="Dispatch agents in this sweep, whatever the time of day.",
    )
    return parser


def _request(args: argparse.Namespace) -> TaskSweepRequest:
    """The request, with only the flags given set, so no flag sends ``{}``."""
    flags = {name: True for name in ("refine", "agents") if getattr(args, name)}
    return TaskSweepRequest(**flags)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: KaianoApiClient | None = None,
) -> int:
    """Run the command. Returns the exit code."""
    request = _request(_parser().parse_args(argv))
    api = client or KaianoApiClient.from_env(MACHINE_NAME)
    try:
        accepted = api.request_sweep(request)
    except KaianoApiError as exc:
        print(
            f"request-sweep: no sweep queued: "
            f"{exc.status_code} {exc.message.splitlines()[0] if exc.message else ''}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "accepted": accepted.accepted,
                "message_id": accepted.message_id,
                "refine": request.refine,
                "agents": request.agents,
            }
        )
    )
    return 0


def cli() -> None:
    """The ``request-sweep`` console script."""
    sys.exit(main())
