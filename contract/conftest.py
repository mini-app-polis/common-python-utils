"""The contract suite's one message: its result, posted once after the run.

The development API drops every message except this one, which the
contract-suite machine sends marked ``contract_suite_result``. It is posted
whether the run passed or failed. A run stopped before it finished (a guard
refusing the configuration, an interrupt) or one that only collected posts
nothing.
"""

from __future__ import annotations

import os
from collections import Counter
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import (
    NOTIFY_QUEUED,
    NotificationResult,
    NotifyRequest,
)

from .harness import MACHINE_NAME, ContractConfigError, load_config

#: Test outcomes for the run, tallied from each test's reports.
_outcomes: Counter[str] = Counter()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # A test is failed if any phase failed, skipped if setup or the call
    # skipped, and passed if its call passed.
    if report.failed:
        _outcomes["failed"] += 1
    elif report.skipped:
        _outcomes["skipped"] += 1
    elif report.when == "call":
        _outcomes["passed"] += 1


#: What started the run, by GitHub Actions' event name; anything else
#: (another event, or a local run with none) is a run by hand.
STARTED_BY = {
    "pull_request": "promote pull request",
    "repository_dispatch": "dev API deploy",
}
BY_HAND = "by hand"


def started_by(event_name: str) -> str:
    """What started the run, in words, from ``GITHUB_EVENT_NAME``."""
    return STARTED_BY.get(event_name.strip(), BY_HAND)


def deployed_commit(api_url: str) -> str:
    """The commit the API at ``api_url`` was built from, read from its
    public ``GET /version``; empty when it gives none or cannot be reached.

    Only a run the API itself asked for is told its commit
    (``CONTRACT_API_SHA``); the others ask. A failure here never stops the
    run or its result: the message then says the commit is unknown.
    """
    parts = urlsplit(api_url)
    try:
        response = httpx.get(f"{parts.scheme}://{parts.netloc}/version", timeout=10)
        response.raise_for_status()
        commit = response.json().get("commit")
    except Exception:  # noqa: BLE001 — the result must still be posted
        return ""
    return commit.strip() if isinstance(commit, str) else ""


def result_message(
    host: str, api_sha: str, outcomes: Counter[str], started: str = BY_HAND
) -> str:
    """The result line: the verdict, the API it ran against and at which
    commit, what started the run, and the counts."""
    verdict = "failed" if outcomes["failed"] else "passed"
    commit = api_sha[:7] if api_sha else "an unknown commit"
    return (
        f"Contract suite {verdict} against {host} at {commit} ({started}): "
        f"{outcomes['passed']} passed, {outcomes['failed']} failed, "
        f"{outcomes['skipped']} skipped."
    )


def result_note(result: NotificationResult, content: str) -> str:
    """The terminal line for the API's answer to the posted result.

    Queued is accepted: the API will send it, so it is not reported as not
    forwarded.
    """
    if result.forwarded:
        return f"contract suite result posted: {content}"
    if result.reason == NOTIFY_QUEUED:
        return f"contract suite result queued for posting: {content}"
    return f"contract suite result not forwarded ({result.reason}): {content}"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.option.collectonly or exitstatus not in (
        pytest.ExitCode.OK,
        pytest.ExitCode.TESTS_FAILED,
    ):
        return
    try:
        config = load_config()
    except ContractConfigError:
        return

    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    api_sha = os.environ.get("CONTRACT_API_SHA", "").strip()
    content = result_message(
        config.host,
        api_sha or deployed_commit(config.api_url),
        _outcomes,
        started_by(os.environ.get("GITHUB_EVENT_NAME", "")),
    )
    client = KaianoApiClient(
        base_url=config.api_url, api_key=config.api_key, machine_name=MACHINE_NAME
    )
    try:
        result = client.send_notification(
            NotifyRequest(
                content=content, username=MACHINE_NAME, contract_suite_result=True
            )
        )
    except (KaianoApiError, httpx.HTTPError, ValidationError) as exc:
        note = f"contract suite result not posted: {exc}"
    else:
        note = result_note(result, content)
    if terminal is not None:
        terminal.ensure_newline()
        terminal.write_line(note)
