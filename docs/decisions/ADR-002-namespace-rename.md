# ADR-002: Rename import namespace from kaiano to mini_app_polis

Date: 2026-03-27

## Status

Accepted. Shipped in v2.0.0.

## Context

The library's import namespace was originally `kaiano`, reflecting its
origin as a personal utility library. As the ecosystem grew into a
multi-service platform (MiniAppPolis) consumed by cogs, API services,
and tooling, the personal-name prefix became a poor fit:

- The library was no longer coupled to a single project or owner.
- New contributors and future readers would see `kaiano.*` imports
  without context for what the namespace meant.
- The distribution had already been renamed to `common-python-utils`
  (see ADR-001), so the `kaiano` import was the last remaining piece
  of the old branding.

The question was whether the break was worth the coordinated update
across every consumer repo.

## Decision

Rename the import namespace from `kaiano` to `mini_app_polis` in a
single breaking v2.0.0 release. Every consumer updates imports from
`from kaiano.x` to `from mini_app_polis.x` in lockstep.

Distribution name remains `common-python-utils` (unchanged from ADR-001).

## Consequences

- Every consumer repo needed a coordinated bump to v2.0.0 and an import
  sweep. This was completed during the v2.0.0 rollout.
- The namespace now communicates the ecosystem, not a person. Future
  consumers onboard with less context.
- Older documentation, old commits, and external references to
  `kaiano.*` become dated. CHANGELOG and README cover the rename
  clearly enough that this is not a burden.
- The split between distribution name and import name (ADR-001) remains.
  This ADR addresses only the import namespace.
