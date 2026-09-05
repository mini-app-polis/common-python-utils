"""mini_app_polis — common Python utilities for the MiniAppPolis ecosystem.

Install name:  miniapppolis-common-utils   (the distribution, on PyPI)
Import name:   mini_app_polis               (the package, in your source)

The two differ deliberately. The distribution carries the ecosystem prefix
because it is published in a public registry, where `common-python-utils`
is a name anyone could reasonably want. The import namespace is what every
consumer already writes, so renaming it would break them all and buy
nothing. Repository: mini-app-polis/common-python-utils.

Install:
    # pyproject.toml
    dependencies = ["miniapppolis-common-utils>=5.0,<6"]

Import:
    from mini_app_polis.api import KaianoApiClient
    from mini_app_polis.google import GoogleAPI
    from mini_app_polis import logger

Everything reachable from this module is resolved lazily via
``__getattr__``. Submodules in this package pull in heavyweight or
optional third-party dependencies (httpx, tenacity, google-api-python-client,
gspread, …), and consumers that only want one of them must not pay the
import cost of all of them. Add new public names to ``_LAZY_ATTRS``
rather than importing them at module scope.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import config as config
    from .serve_resilience import serve_with_retry as serve_with_retry

__all__ = ["config", "serve_with_retry"]

#: Public name → ``(module, attribute-or-None)``. ``None`` means the
#: name resolves to the module itself rather than an attribute on it.
_LAZY_ATTRS: dict[str, tuple[str, str | None]] = {
    "config": ("mini_app_polis.config", None),
    "serve_with_retry": ("mini_app_polis.serve_resilience", "serve_with_retry"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    module = importlib.import_module(module_name)
    return module if attr is None else getattr(module, attr)


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS})
