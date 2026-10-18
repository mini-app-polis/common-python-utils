"""One way for a service to start Sentry, and to say so when it can't.

Every Python service used to initialise Sentry itself, and each one either
passed an empty DSN to ``sentry_sdk.init`` (a documented no-op) or guarded
it with ``if not dsn: return`` (the same no-op written out). Either way the
service ran with every exception going nowhere and nothing recording that
Layer 3 was off. That is how db-ops ran its crons without Sentry until the
names were audited by hand (ecosystem-standards CD-038).

:func:`init_sentry` owns that branch for the fleet:

- A DSN is present: Sentry starts, labeled with the environment, and the
  ``service`` tag tells this service's events apart in a shared project.
- No DSN in production: an ERROR naming the variable it read. The service
  still starts — CD-018's telemetry carve-out: an observability gap must
  not become an outage.
- No DSN anywhere else (local runs, tests, development): silent. That is
  normal there.

Production is read through :mod:`mini_app_polis.environment`, never a
hand-rolled check, so this agrees with every other gate in the fleet.

``sentry_sdk`` is still not a dependency of this library (see ``_sentry``):
a service that calls this already depends on it. If it is somehow missing
in production, that is the same gap and gets the same ERROR.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from . import _sentry, logger
from .environment import Environment, current_environment

__all__ = ["DEFAULT_DSN_VAR", "init_sentry"]

#: The shared cogs project's DSN, under the name every cog reads. The APIs
#: pass their own (``SENTRY_DSN_APIS``).
DEFAULT_DSN_VAR = "SENTRY_DSN_COGS"


def init_sentry(
    service: str,
    *,
    dsn_var: str = DEFAULT_DSN_VAR,
    dsn: str | None = None,
    integrations: Sequence[Any] = (),
    traces_sample_rate: float = 0.0,
    release: str | None = None,
) -> bool:
    """Start Sentry for ``service``; return whether it is on.

    ``dsn_var`` is the variable read, and the name an ERROR gives when it is
    empty. Pass ``dsn`` when the value comes from somewhere other than the
    process environment (a Settings object reading a ``.env`` file); the
    message still names ``dsn_var``, which is what someone has to set.

    Call it once per process, at the entry point: at import for a Lambda
    handler (once per cold start), in the lifespan for a FastAPI app.
    Never raises.
    """
    value = (dsn if dsn is not None else os.environ.get(dsn_var) or "").strip()
    environment = current_environment()
    production = environment is Environment.PRODUCTION
    log = logger.get_logger()

    if not value:
        if production:
            log.error(
                "Sentry is off for %s: %s is empty in production, so its "
                "exceptions will not be reported (CD-038)",
                service,
                dsn_var,
            )
        return False

    sdk = _sentry.sentry_sdk
    if sdk is None:
        if production:
            log.error(
                "Sentry is off for %s: %s is set but sentry_sdk is not "
                "installed, so its exceptions will not be reported (CD-038)",
                service,
                dsn_var,
            )
        return False

    options: dict[str, Any] = {
        "dsn": value,
        "environment": environment.value,
        "traces_sample_rate": traces_sample_rate,
    }
    if integrations:
        options["integrations"] = list(integrations)
    if release:
        options["release"] = release
    try:
        sdk.init(**options)
        # Shared projects: the tag is what tells this service's events apart.
        sdk.get_global_scope().set_tag("service", service)
    except Exception:  # noqa: BLE001 — telemetry must not stop the service
        log.error("Sentry is off for %s: initialisation failed", service, exc_info=True)
        return False
    return True
