"""The commands the package installs, and that each one resolves.

Every consumer's CI runs ``uvx --from miniapppolis-common-utils facts check``,
and working sessions run ``file-task`` and ``request-sweep``. 8.0.0 shipped
with ``[project.scripts]`` dropped by an edit to the dependencies above it,
and every consumer's build failed on "does not provide any executables".
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"

COMMANDS = {"facts", "file-task", "request-sweep", "check-doppler-keys"}


def _scripts() -> dict[str, str]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"].get("scripts", {})


def test_every_command_is_declared() -> None:
    assert set(_scripts()) >= COMMANDS


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_each_command_points_at_a_callable(command: str) -> None:
    module, _, attr = _scripts()[command].partition(":")
    assert callable(getattr(importlib.import_module(module), attr))
