from __future__ import annotations

import importlib
import sys


def test_config_imports_cleanly() -> None:
    # tests/mini_app_polis/google/conftest.py stubs mini_app_polis.config for the whole session;
    # load the real implementation for this smoke test.
    sys.modules.pop("mini_app_polis.config", None)
    importlib.invalidate_caches()
    from mini_app_polis import config

    assert hasattr(config, "TIMEZONE")
    assert hasattr(config, "SPOTIPY_CLIENT_ID")
