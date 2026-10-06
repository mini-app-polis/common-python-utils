"""Load a Lambda worker's secrets from SSM Parameter Store into ``os.environ``.

Why this exists. The Lambda cogs used to receive their secrets as Terraform
variables, which put every one of them in plaintext Terraform state and in
every plan. Now Doppler syncs the ecosystem's ``prd`` config into Parameter
Store under one prefix, Terraform declares *which* parameters each worker
may read — names only, never values — and the worker loads them itself at
cold start. Nothing secret passes through Terraform.

The worker's environment, set by Terraform, says what to load:

``SSM_PREFIX``
    The path Doppler syncs to, with leading and trailing slashes:
    ``/mini-app-polis/prd/``.
``SSM_PARAMETERS``
    JSON object, environment variable name → parameter name under the
    prefix. Every one must exist; a missing one fails the cold start.
``SSM_OPTIONAL_PARAMETERS``
    The same shape, for settings the code has its own default for. A
    missing one is left unset rather than set to ``""`` — Parameter Store
    cannot hold an empty value, and Doppler skips them when it syncs, so
    absent is how "not configured" arrives.

Where to call it. At import of the cog's package — its ``__init__.py`` —
because Python runs that before any line of the handler module. Several
modules read the environment when they are imported (``LOGGING_LEVEL`` in
``mini_app_polis.logger``, folder ids in ``config`` modules, Sentry's DSN at
``sentry_sdk.init``), and a secret that arrives after them arrives too late.

With none of the three variables set it does nothing, so a local run under
``doppler run`` and the test suite are unaffected.

A parameter's value replaces anything already in the environment under that
name: Parameter Store is the source of truth for the names it is asked for.

Refreshing. A warm Lambda container keeps what it loaded at cold start, so
a value changed in Doppler did not reach a worker until a deploy. A worker
also calls ``load_secrets(refresh=True)`` at the top of each invocation: the
declared parameters are read again, changed values replace the old ones, an
optional parameter that has gone is unset, and ``LOGGING_LEVEL`` is
re-applied (:func:`mini_app_polis.logger.apply_level`). A change in Doppler
then applies at the next invocation after the sync. Only settings a module
reads at import — Sentry's DSN — still need a cold start.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from typing import Any

PREFIX_VAR = "SSM_PREFIX"
REQUIRED_VAR = "SSM_PARAMETERS"
OPTIONAL_VAR = "SSM_OPTIONAL_PARAMETERS"

#: GetParameters accepts at most ten names per call.
_BATCH_SIZE = 10

#: Stdlib logging, not ``mini_app_polis.logger``: that module reads
#: ``LOGGING_LEVEL`` when imported, and that value may be one of the
#: parameters this is about to load.
_log = logging.getLogger(__name__)

_loaded = False

#: Optional variables this module set, so a refresh can unset one whose
#: parameter has since been deleted — without touching a variable the
#: function's own environment provides.
_set_optional: set[str] = set()


class MissingParameterError(RuntimeError):
    """A required parameter is not in Parameter Store."""


def load_secrets(*, client: Any | None = None, refresh: bool = False) -> list[str]:
    """Load the declared parameters into ``os.environ``.

    Once per process by default; with ``refresh=True``, again on every call
    (see the module docstring). Returns the environment variable names that
    were set — names only, so it is safe to log. ``client`` is an SSM
    client; tests pass a fake, and the default is ``boto3.client("ssm")``.
    boto3 ships in the Lambda runtime and is imported only when there is
    something to load, so this package takes no dependency on it.

    Raises :class:`MissingParameterError` naming every missing required
    parameter, before setting anything: a worker either runs with all of
    its secrets or does not run. On a refresh, a Parameter Store call that
    fails is logged and the values already loaded are kept — an SSM blip
    must not fail a run the last values would have served.
    """
    global _loaded
    if _loaded and not refresh:
        return []

    required = _read_map(REQUIRED_VAR)
    optional = _read_map(OPTIONAL_VAR)
    if not required and not optional:
        _loaded = True
        return []

    overlap = sorted(required.keys() & optional.keys())
    if overlap:
        raise ValueError(
            f"{', '.join(overlap)} declared in both {REQUIRED_VAR} and {OPTIONAL_VAR}"
        )

    prefix = os.environ.get(PREFIX_VAR, "")
    if not (prefix.startswith("/") and prefix.endswith("/") and len(prefix) > 1):
        raise ValueError(
            f"{PREFIX_VAR} must be a path with leading and trailing slashes, "
            f"such as /mini-app-polis/prd/; got {prefix!r}"
        )

    if client is None:
        client = importlib.import_module("boto3").client("ssm")

    wanted = {**required, **optional}
    refreshing = _loaded
    try:
        values = _fetch(client, sorted({prefix + name for name in wanted.values()}))
    except Exception as exc:
        if not refreshing:
            raise
        _log.warning(
            "ssm_secrets: refresh failed, keeping the values already loaded: %s",
            exc,
        )
        return []

    missing = sorted(
        f"{env} ({prefix}{name})"
        for env, name in required.items()
        if prefix + name not in values
    )
    if missing:
        raise MissingParameterError(
            "Required parameters are not in Parameter Store: "
            + ", ".join(missing)
            + ". Check that Doppler holds them with non-empty values and that "
            "the sync has run."
        )

    loaded = []
    changed = []
    for env, name in wanted.items():
        value = values.get(prefix + name)
        if value is not None:
            if os.environ.get(env) != value:
                changed.append(env)
            os.environ[env] = value
            loaded.append(env)
            if env in optional:
                _set_optional.add(env)
        elif env in _set_optional:
            # Deleted from Doppler since the last load: it goes here too.
            os.environ.pop(env, None)
            _set_optional.discard(env)
            changed.append(env)

    skipped = sorted(env for env in optional if env not in loaded)
    if not refreshing:
        _log.info(
            "ssm_secrets: loaded %d parameter(s) from %s%s",
            len(loaded),
            prefix,
            f"; optional and absent: {', '.join(skipped)}" if skipped else "",
        )
    elif changed:
        _log.info("ssm_secrets: refreshed %s", ", ".join(sorted(changed)))
    _loaded = True
    _apply_logging_level()
    return sorted(loaded)


def _apply_logging_level() -> None:
    """Re-apply LOGGING_LEVEL, which may be one of the parameters just loaded.

    Imported here rather than at module level, for the reason given on
    ``_log`` above: the logger reads LOGGING_LEVEL when it is imported.
    """
    apply = getattr(
        importlib.import_module("mini_app_polis.logger"), "apply_level", None
    )
    if apply is not None:
        apply()


def _read_map(var: str) -> dict[str, str]:
    """Parse one of the mapping variables; empty when unset."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{var} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in parsed.items()
    ):
        raise ValueError(
            f"{var} must be a JSON object of environment variable name to "
            "parameter name, both non-empty strings"
        )
    bad = sorted(v for v in parsed.values() if v.startswith("/"))
    if bad:
        raise ValueError(
            f"{var} holds names relative to {PREFIX_VAR}, without a leading "
            f"slash; got {', '.join(bad)}"
        )
    return parsed


def _fetch(client: Any, names: list[str]) -> dict[str, str]:
    """Full parameter name → decrypted value, for the names that exist."""
    values: dict[str, str] = {}
    for start in range(0, len(names), _BATCH_SIZE):
        response = client.get_parameters(
            Names=names[start : start + _BATCH_SIZE], WithDecryption=True
        )
        # Names that do not exist come back in InvalidParameters rather than
        # as an error; the caller decides whether that matters.
        for parameter in response.get("Parameters", []):
            values[parameter["Name"]] = parameter["Value"]
    return values
