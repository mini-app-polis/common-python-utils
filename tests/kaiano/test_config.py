from __future__ import annotations

import importlib
import sys


def test_config_imports_cleanly() -> None:
    # tests/kaiano/google/conftest.py stubs kaiano.config for the whole session;
    # load the real implementation for this smoke test.
    sys.modules.pop("kaiano.config", None)
    importlib.invalidate_caches()
    from kaiano import config

    assert hasattr(config, "TIMEZONE")
    assert hasattr(config, "SPOTIPY_CLIENT_ID")
