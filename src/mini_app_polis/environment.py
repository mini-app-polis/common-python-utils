"""Which environment this process is in, and what it is allowed to touch.

Every service in the fleet answers "which environment am I?" differently
today — some read ``RAILWAY_ENVIRONMENT``, some hardcode ``"production"``,
some never ask. This module is the single answer, so a service that needs
to know imports it rather than inventing a fourth convention.

Two things live here, and they are deliberately separate.

``current_environment()`` reports where the process is running. It is a
fact about the deployment, and business logic does not branch on it.

``effect_enabled()`` reports whether one named outbound effect may fire.
Callers gate on *that*, so dev and production run the same code path and
differ only in what the gate returns. An ``if env == PRODUCTION`` spread
through business logic means the branch that matters in production is the
one never exercised in dev.

Effects are gated only where the target has no environment of its own.
Sentry and Discord both carry the environment on the message and stay on
everywhere; Healthchecks.io has one check and Prefect Cloud has one
workspace across both environments, so dev must not reach them.

Resolution is fail-closed: an environment this module cannot name is
treated as non-production. That direction is chosen because it announces
itself — a production service misread as development stops pinging
Healthchecks, its check goes red, and the mistake surfaces within the
period. That only holds because the ping is gated through here like
everything else.
"""

from __future__ import annotations

import os
from enum import StrEnum


class Environment(StrEnum):
    """Where this process is running."""

    PRODUCTION = "production"
    DEVELOPMENT = "development"
    LOCAL = "local"


class Effect(StrEnum):
    """One named outbound effect that a non-production process must not perform.

    The member name derives its override variable — ``PREFECT_TRIGGER`` ->
    ``PREFECT_TRIGGER_ENABLED`` — so there is one rule rather than a
    mapping to keep in step on both sides, the same convention
    ``machine_key_env_var`` uses for API keys.

    Only effects whose target has no environment split belong here. Adding
    Sentry or Discord would turn a label into a silence, and the
    environment you most want error reporting in is the one you are
    actively breaking.
    """

    PREFECT_TRIGGER = "prefect_trigger"
    #: Registering deployments with Prefect Cloud and running the serve
    #: loop that polls them. Separate from PREFECT_TRIGGER because they are
    #: different reaches into the same workspace — triggering fires one run,
    #: serving claims every run of a deployment — and because a deliberate
    #: dev test of one should not silently unmute the other.
    #:
    #: Ungated, two environments running the same cog register the *same*
    #: deployment name in the one Prefect Cloud workspace and both poll it,
    #: so whichever runner claims a scheduled run executes it. A production
    #: run claimed by the development container resolves development
    #: correctly and writes its results to the development API — which is
    #: not a resolution bug and does not announce itself as one.
    PREFECT_SERVE = "prefect_serve"
    HEALTHCHECKS = "healthchecks"


#: Spellings seen across Railway environment names, Doppler configs and
#: local ``.env`` files. Anything not listed resolves to DEVELOPMENT.
_ALIASES: dict[str, Environment] = {
    "production": Environment.PRODUCTION,
    "prod": Environment.PRODUCTION,
    "prd": Environment.PRODUCTION,
    "development": Environment.DEVELOPMENT,
    "develop": Environment.DEVELOPMENT,
    "dev": Environment.DEVELOPMENT,
    "local": Environment.LOCAL,
}

#: Checked in order. ``ENVIRONMENT`` first, so a laptop, a test harness or
#: a one-off container can say plainly what it is. Railway's own variables
#: second, so a deployed service is labeled correctly with nothing set by
#: hand — which is what makes a newly duplicated environment safe on its
#: first boot rather than once someone remembers. Both Railway spellings
#: are checked because the fleet already reads the older one.
_SOURCES: tuple[str, ...] = (
    "ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT",
)

#: Suffix on environment-specific variable names. Production is unsuffixed
#: because every consumer and every existing config already writes the bare
#: name; suffixing it would be a fleet-wide rename for no gain.
_SUFFIX: dict[Environment, str] = {
    Environment.PRODUCTION: "",
    Environment.DEVELOPMENT: "_DEV",
    Environment.LOCAL: "_DEV",
}

_TRUE = frozenset({"1", "true", "yes", "on"})


def resolve() -> tuple[Environment, str]:
    """Return the current environment and the variable it was read from.

    Nothing is cached. These are read at most a few times per process, and
    caching would make the module untestable with ``monkeypatch.setenv``.
    """
    for name in _SOURCES:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        return _ALIASES.get(raw.lower(), Environment.DEVELOPMENT), name
    return Environment.LOCAL, "default"


def current_environment() -> Environment:
    """Where this process is running."""
    return resolve()[0]


def is_production() -> bool:
    """Whether this process is running in production."""
    return current_environment() is Environment.PRODUCTION


def effect_enabled(effect: Effect) -> bool:
    """Whether ``effect`` may actually fire in this process.

    An explicit ``<EFFECT>_ENABLED`` wins over the derived default. Set it
    to ``true`` in production so production behavior never depends on the
    fail-closed default, and so a single effect can be enabled in dev for
    a deliberate end-to-end test without unmuting the others.
    """
    raw = (os.environ.get(f"{effect.name}_ENABLED") or "").strip().lower()
    if raw:
        return raw in _TRUE
    return is_production()


def env_var_name(name: str) -> str:
    """The variable actually read for ``name`` in this environment.

    ``env_var_name("KAIANO_API_BASE_URL")`` is ``KAIANO_API_BASE_URL`` in
    production and ``KAIANO_API_BASE_URL_DEV`` everywhere else. Exposed so
    that an error message can name the variable a reader needs to set,
    rather than making them work out the suffix — and so no caller
    reconstructs that suffix for itself.
    """
    return f"{name}{_SUFFIX[current_environment()]}"


def env_var(name: str) -> str:
    """Value of an environment-specific variable, given its production name.

    ``env_var("KAIANO_API_BASE_URL")`` reads ``KAIANO_API_BASE_URL`` in
    production and ``KAIANO_API_BASE_URL_DEV`` everywhere else.

    There is deliberately no fallback to the unsuffixed variable when the
    suffixed one is missing. Falling back would point a dev process at
    production the moment a variable was forgotten, which is the failure
    this exists to prevent. An empty value that stops the call is the safe
    outcome, and the startup line names it.
    """
    return (os.environ.get(env_var_name(name)) or "").strip()


def api_base_url() -> str:
    """Base URL of the Kaiano API for this environment. May be empty."""
    return env_var("KAIANO_API_BASE_URL")


def summary() -> str:
    """One line naming the environment and the state of every gate.

    Log this once at startup. It is the only place the whole configuration
    is visible at a glance, and it is what you read after a deploy instead
    of assuming — a cog pointed at the wrong API is one line in the deploy
    log rather than a dev finding discovered in the production channel.
    """
    env, source = resolve()
    gates = " ".join(
        f"{effect.value}={'on' if effect_enabled(effect) else 'off'}"
        for effect in Effect
    )
    return (
        f"environment={env.value} (from {source}) {gates} "
        f"api_base_url={api_base_url() or '<unset>'}"
    )
