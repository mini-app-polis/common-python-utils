"""The base install: the modules every cog uses load nothing an extra brings.

Since 8.0 the Google, Spotify, MP3 and LLM packages come only with their
extras. A core module that imported one of them, even indirectly, would fail
in every cog that left that extra out, so each is imported here in a fresh
interpreter and the packages it loaded are checked.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

#: Top-level packages that only an extra installs.
EXTRA_PACKAGES = {
    "google",
    "googleapiclient",
    "gspread",
    "spotipy",
    "mutagen",
    "music_tag",
    "acoustid",
    "musicbrainzngs",
    "openai",
    "anthropic",
    "jsonschema",
    "sqlalchemy",
    "fastapi",
}

#: Modules a cog uses with no extra at all.
CORE_MODULES = [
    "mini_app_polis",
    "mini_app_polis.api",
    "mini_app_polis.api.contract",
    "mini_app_polis.asana",
    "mini_app_polis.config",
    "mini_app_polis.deadline",
    "mini_app_polis.discord",
    "mini_app_polis.doppler",
    "mini_app_polis.environment",
    "mini_app_polis.facts",
    "mini_app_polis.lambda_worker",
    "mini_app_polis.logger",
    "mini_app_polis.pipeline_status",
    "mini_app_polis.timing",
]


@pytest.mark.parametrize("module", CORE_MODULES)
def test_a_core_module_loads_no_extras_package(module: str) -> None:
    code = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not loaded & EXTRA_PACKAGES, f"{module} loaded {loaded & EXTRA_PACKAGES}"
