"""mini_app_polis — shared Python utilities (import namespace).

**Package identity**

* **PyPI / dependency name:** ``common-python-utils``
* **Import namespace:** ``mini_app_polis``
* **Repository:** https://github.com/mini-app-polis/common-python-utils

**Pinning in pyproject.toml** (example)::

    [tool.uv.sources]
    common-python-utils = {
        git = "https://github.com/mini-app-polis/common-python-utils.git",
        tag = "v2.1.0",
    }

    [project]
    dependencies = [
        "common-python-utils",
    ]

**Imports** (examples)::

    from mini_app_polis.api import KaianoApiClient
    from mini_app_polis.google import GoogleAPI

**Naming:** The ``mini_app_polis`` package name is a legacy artefact. Renaming to
something like ``kaiano-common-utils`` / ``kaiano_common_utils`` would better
match ecosystem convention but requires a **semver major** and coordinated
updates across consumers (``kaianolevine-api``, ``deejay-set-processor-dev``,
``evaluator-cog``, etc.). Do not rename in a patch or minor release.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import config as config

__all__ = ["config"]


def __getattr__(name: str) -> ModuleType:
    if name == "config":
        return importlib.import_module("mini_app_polis.config")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
