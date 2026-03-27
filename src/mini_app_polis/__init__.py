"""mini_app_polis — common Python utilities.

Install: git+https://github.com/mini_app_polislevine/common-python-utils@<tag>
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
