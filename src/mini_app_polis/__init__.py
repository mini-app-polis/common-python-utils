"""mini_app_polis — common Python utilities for the MiniAppPolis ecosystem.

Install name:  common-python-utils
Import name:   mini_app_polis

Install via uv:
    mini_app_polis = { git = "https://github.com/mini-app-polis/common-python-utils", tag = "v2.x.x" }

Import:
    from mini_app_polis.api import KaianoApiClient
    from mini_app_polis.google import GoogleAPI
    from mini_app_polis import logger
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
