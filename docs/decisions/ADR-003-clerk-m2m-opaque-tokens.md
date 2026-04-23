# ADR-003: Clerk M2M auth with opaque tokens

Date: 2026-04-18

## Status

Accepted. Shipped across v2.2.0 - v2.5.0.

## Context

Before Project Keystone, internal service-to-service calls authenticated
using an `X-Owner-Id` header plus a static shared key. This worked but
had three shortcomings:

- The static key lived in environment variables across every cog and
  every deploy target, creating fragile sprawl.
- Revocation required coordinated redeploys.
- Every service trusted every other service equally - no per-caller
  attribution, no granularity for audit or throttling.

Project Keystone's plan was to replace this with Clerk-issued
machine-to-machine JWTs, verified via Clerk's JWKS on the server side.
The library (`common-python-utils`) owns the client side of that
contract: cogs and services use `KaianoApiClient` from this library to
call api-kaianolevine-com, so the client must acquire a token, cache
it until expiry, and attach it to outgoing requests.

During implementation, the team considered two token formats:

- **RS256 JWTs verified via Clerk JWKS.** Standard, well-documented,
  verification is fully offline once the JWKS is cached.
- **Opaque M2M tokens verified by calling Clerk.** Shorter, no key
  rotation complexity for the consumer, but every request costs a
  Clerk round-trip unless cached carefully.

The library's role made the decision. As the client side, it needs a
predictable, tight interface: acquire a token, cache it, attach it.
Opaque tokens fit that shape. The server side (api-kaianolevine-com)
carries the verification logic and its caching.

## Decision

Adopt Clerk M2M opaque tokens as the standard internal auth mechanism.
`KaianoApiClient.from_env()` acquires a token from Clerk on
construction, caches it until expiry, and attaches it as a bearer
token on every outgoing request. The `X-Owner-Id` header is removed
from all internal calls; the owner identity is carried in the token's
`sub` claim.

This ADR covers the library-side implementation only. Server-side
verification and rollout coordination are covered in
api-kaianolevine-com's own ADR trail and in the Keystone rollout
notes.

## Consequences

- Every consumer that upgrades to v2.2.0+ gets Clerk auth automatically
  through `KaianoApiClient.from_env()` - no per-consumer code change
  beyond the version bump.
- Static internal API keys are removed from the ecosystem. The only
  credentials cogs still need are Clerk publishable + secret keys,
  which Doppler already manages.
- Token acquisition adds a Clerk round-trip on client startup. In
  practice this runs once per cog process and is cached; no measurable
  impact on flow runtime.
- If Clerk is unreachable at startup, the client fails loudly rather
  than falling back to legacy auth. Keystone's legacy feature flag
  (`keystone.legacy_auth_enabled`) is server-side only and can be
  flipped back if needed during the cutover window.
