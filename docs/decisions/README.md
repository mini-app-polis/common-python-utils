# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for this
repository. ADRs document significant architectural decisions, the
context around them, and their consequences.

## Format

Each ADR is a markdown file named `ADR-NNNN-short-slug.md` where `NNNN`
is a zero-padded four-digit sequence number starting at `0001`. This
matches the ecosystem-standards DOC-005 specification. An ADR in another
repository is cited with that repository's name first, as in
`ecosystem-standards ADR-0009`.

Each ADR uses three sections: **Context** (what forces motivated the
decision), **Decision** (what change is being made), and
**Consequences** (what becomes easier or harder).

## Index

- [ADR-0001: Split package identity](./ADR-0001-split-package-identity.md)
- [ADR-0002: Namespace rename from kaiano to mini_app_polis](./ADR-0002-namespace-rename.md)
- [ADR-0003: Clerk M2M opaque token auth](./ADR-0003-clerk-m2m-opaque-tokens.md) — **superseded** Sep 2026 by ecosystem-standards ADR-0008 (named machine keys); the M2M path was removed from this library in v4.0.0
