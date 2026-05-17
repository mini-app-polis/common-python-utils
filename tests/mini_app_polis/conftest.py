"""Test fixtures shared across ``tests/mini_app_polis``.

The pipeline_status module lazily imports ``prefect`` (its consumers are
Prefect-driven cogs, but ``mini_app_polis`` itself doesn't take Prefect
as a runtime dependency). For unit tests we install a minimal Prefect
stub so the lazy-import paths can be exercised and patched without
pulling in the real package.
"""

from __future__ import annotations

import sys
import types


def _install_prefect_stub() -> None:
    """Install a minimal ``prefect`` stub if the real package is absent.

    Only the surface area used by ``mini_app_polis.pipeline_status`` is
    provided: ``prefect.get_run_logger`` and ``prefect.runtime.flow_run``
    (which exposes a default ``id = None``).
    """
    if "prefect" in sys.modules:
        return

    prefect_mod = types.ModuleType("prefect")

    def get_run_logger():  # pragma: no cover - default raises so the
        raise RuntimeError("no flow run context")  # fallback logger path runs

    prefect_mod.get_run_logger = get_run_logger  # type: ignore[attr-defined]

    runtime_mod = types.ModuleType("prefect.runtime")
    flow_run_mod = types.ModuleType("prefect.runtime.flow_run")
    flow_run_mod.id = None  # type: ignore[attr-defined]
    runtime_mod.flow_run = flow_run_mod  # type: ignore[attr-defined]
    prefect_mod.runtime = runtime_mod  # type: ignore[attr-defined]

    sys.modules["prefect"] = prefect_mod
    sys.modules["prefect.runtime"] = runtime_mod
    sys.modules["prefect.runtime.flow_run"] = flow_run_mod


_install_prefect_stub()
