# ADR-001: Split package identity

Date: 2026-03-27

## Status

Accepted

## Context

This library is published as a distribution named `common-python-utils`
but its import namespace is `mini_app_polis`. Import sites across the
ecosystem look like:

    from mini_app_polis import logger
    from mini_app_polis.api import KaianoApiClient

Ecosystem-standards rule PY-011 expects the distribution name and the
import package name to match. This split predates the rule and the
question is whether to rename the import namespace, rename the
distribution, or keep the split.

Three forces act on this choice:

- The `mini_app_polis` namespace is stable and appears in every consumer
  repo (every cog, every API service, every tool in the ecosystem).
- The distribution name `common-python-utils` is the user-facing label
  on PyPI and in `pyproject.toml` dependency lists - it communicates
  what the package is for, which `mini_app_polis` does not.
- Renaming the import namespace is a breaking change for every
  downstream consumer. Renaming the distribution changes install
  commands but not source code.

## Decision

Keep the split. The distribution is `common-python-utils`; the import
is `mini_app_polis`. Both names serve distinct purposes - the
distribution name for discovery, the import name for ecosystem branding
- and the cost of unifying them outweighs the benefit of PY-011
conformance.

This decision is codified as a PY-011 exemption in `evaluator.yaml`.

## Consequences

- Every consumer's dependency list says `common-python-utils` while
  every import says `mini_app_polis`. This is a persistent onboarding
  friction point for new developers.
- PY-011 cannot be cleanly audited for this repo; the exemption must be
  honored by every future version of the checker.
- If ecosystem-wide rebranding happens, the rename cost is paid in one
  breaking release rather than being amortized. ADR-002 covers one
  prior such rename (`kaiano` -> `mini_app_polis`) and sets precedent
  for how to handle the next if it comes.
