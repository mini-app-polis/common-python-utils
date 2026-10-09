"""Doppler, the ecosystem's one secret store: checking it and writing to it.

Two jobs, both small.

**Checking a repo's contract against Doppler.** Every repo lists the names it
reads in ``.env.example``: an uncommented ``NAME=`` line is required, a
commented ``# NAME=`` line is optional (the code has a default). The repo's
``doppler.yaml`` pins it to a project, and local runs always use the shared
``dev`` config, never ``prd``. ``check-doppler-keys``, a console script this
package installs, compares the two and lists what ``dev`` is missing::

    uv run check-doppler-keys

It prints names only, never values, and exits 1 when a required name is
missing, so it can gate a make target or a pre-commit hook. It needs the
Doppler CLI and a ``doppler login``; the test suites never call it.

**Writing a secret.** A service that renews a credential (Spotify's refresh
token, say) writes the new value back to Doppler with :class:`DopplerClient`,
which calls Doppler's API with a service token. A person running a script
by hand uses :func:`set_secrets_with_cli` instead, which goes through their
own ``doppler login``. Either way the value lands in Doppler and reaches
SSM and Railway through the existing syncs; nobody copies it by hand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

#: The config local runs use. Never ``prd``: that one is synced to the
#: platforms, and a laptop has no business reading production credentials.
LOCAL_CONFIG = "dev"

API_URL = "https://api.doppler.com/v3/configs/config/secrets"

_REQUIRED = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_OPTIONAL = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=")
_PROJECT = re.compile(r"^\s*-?\s*project:\s*(\S+)\s*$", re.MULTILINE)


class DopplerError(RuntimeError):
    """Doppler refused a request, or could not be reached."""


# ---------------------------------------------------------------------------
# Checking a repo against dev
# ---------------------------------------------------------------------------


def declared_names(env_example: str) -> tuple[list[str], list[str]]:
    """``(required, optional)`` names from the text of ``.env.example``."""
    required: list[str] = []
    optional: list[str] = []
    for line in env_example.splitlines():
        line = line.strip()
        if m := _REQUIRED.match(line):
            required.append(m.group(1))
        elif m := _OPTIONAL.match(line):
            optional.append(m.group(1))
    return required, optional


def doppler_project(doppler_yaml: str) -> str:
    """The project ``doppler.yaml`` pins a repo to."""
    m = _PROJECT.search(doppler_yaml)
    if not m:
        raise DopplerError("doppler.yaml names no project")
    return m.group(1)


def doppler_names(project: str, config: str = LOCAL_CONFIG) -> set[str]:
    """The secret names in ``project``/``config``, read with the CLI.

    ``--only-names``: the values never leave Doppler.
    """
    if shutil.which("doppler") is None:
        raise DopplerError(
            "The Doppler CLI is not installed: brew install dopplerhq/cli/doppler"
        )
    result = subprocess.run(
        [
            "doppler",
            "secrets",
            "--only-names",
            "--json",
            "--project",
            project,
            "--config",
            config,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DopplerError(f"doppler failed: {result.stderr.strip()}")
    try:
        return set(json.loads(result.stdout))
    except ValueError:
        raise DopplerError("doppler returned output that is not JSON") from None


def check_keys(root: Path) -> int:
    """Report which of ``root``'s required names ``dev`` lacks; 1 if any."""
    required, optional = declared_names((root / ".env.example").read_text())
    project = doppler_project((root / "doppler.yaml").read_text())
    have = doppler_names(project)

    missing = [n for n in required if n not in have]
    absent_optional = [n for n in optional if n not in have]

    print(f"Doppler {project}/{LOCAL_CONFIG}: {len(required)} required names checked.")
    if absent_optional:
        print("Optional, not set (defaults apply):", ", ".join(absent_optional))
    if missing:
        print("MISSING (required):", ", ".join(missing))
        return 1
    print("All required names are present.")
    return 0


def check_keys_main() -> None:
    """``check-doppler-keys``: run from the root of the repo to check."""
    try:
        code = check_keys(Path.cwd())
    except (DopplerError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        code = 2
    sys.exit(code)


# ---------------------------------------------------------------------------
# Writing secrets
# ---------------------------------------------------------------------------


class DopplerClient:
    """Write secrets to one Doppler config through the API.

    For services: authenticate with a service token that has write access
    to the one config it renews (Doppler → the config → Access → Service
    Tokens, "Read/Write"). Scope it to that config alone, so a leaked token
    can change that config and nothing else.
    """

    def __init__(
        self,
        token: str,
        project: str,
        config: str,
        *,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise DopplerError("No Doppler token")
        self._token = token
        self.project = project
        self.config = config
        self._timeout = timeout
        self._http = http

    @classmethod
    def from_env(cls) -> DopplerClient:
        """From ``DOPPLER_TOKEN``, ``DOPPLER_PROJECT`` and ``DOPPLER_CONFIG``.

        The last two are the names Doppler's own syncs inject, so a service
        fed by Doppler already has them; only the token is set by hand.
        """
        try:
            return cls(
                os.environ["DOPPLER_TOKEN"],
                os.environ["DOPPLER_PROJECT"],
                os.environ["DOPPLER_CONFIG"],
            )
        except KeyError as exc:
            raise DopplerError(f"{exc.args[0]} is not set") from None

    def set_secrets(self, secrets: Mapping[str, str]) -> list[str]:
        """Create or update ``secrets``; returns the names written.

        Raises :class:`DopplerError` on any failure. The error never
        carries a value: Doppler's response echoes the secrets, so only
        its status code is kept.
        """
        if not secrets:
            return []
        body = {
            "project": self.project,
            "config": self.config,
            "secrets": dict(secrets),
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        try:
            if self._http is not None:
                resp = self._http.post(API_URL, json=body, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(API_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DopplerError(
                f"Doppler unreachable writing {self.project}/{self.config}: "
                f"{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise DopplerError(
                f"Doppler refused writing {self.project}/{self.config}: "
                f"HTTP {resp.status_code}"
            )
        return sorted(secrets)


def set_secrets_with_cli(
    secrets: Mapping[str, str], *, project: str, config: str
) -> list[str]:
    """Write ``secrets`` through the Doppler CLI and its ``doppler login``.

    For scripts a person runs. Each value goes in on stdin, never on the
    command line where other processes could read it, and the CLI's
    output (which echoes values) is captured and dropped. Raises
    :class:`DopplerError` if the CLI is missing or refuses.
    """
    if shutil.which("doppler") is None:
        raise DopplerError("The Doppler CLI is not installed")
    written: list[str] = []
    for name, value in secrets.items():
        result = subprocess.run(
            [
                "doppler",
                "secrets",
                "set",
                name,
                "--project",
                project,
                "--config",
                config,
            ],
            input=value,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DopplerError(
                f"doppler secrets set {name} failed (exit {result.returncode})"
            )
        written.append(name)
    return written
