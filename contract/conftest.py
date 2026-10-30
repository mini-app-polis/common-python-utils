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

import httpx
import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import NotifyRequest

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


def result_message(host: str, api_sha: str, outcomes: Counter[str]) -> str:
    """The result line: the verdict, the API it ran against, and the counts."""
    verdict = "failed" if outcomes["failed"] else "passed"
    commit = api_sha[:7] if api_sha else "an unknown commit"
    return (
        f"Contract suite {verdict} against {host} at {commit}: "
        f"{outcomes['passed']} passed, {outcomes['failed']} failed, "
        f"{outcomes['skipped']} skipped."
    )


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
    content = result_message(
        config.host, os.environ.get("CONTRACT_API_SHA", "").strip(), _outcomes
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
        note = (
            f"contract suite result posted: {content}"
            if result.forwarded
            else f"contract suite result not forwarded ({result.reason}): {content}"
        )
    if terminal is not None:
        terminal.ensure_newline()
        terminal.write_line(note)
