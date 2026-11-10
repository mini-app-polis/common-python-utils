"""Contract for the API's feature flags.

A flag is a named runtime switch the API keeps: on or off, and optionally a
text value (a mode such as ``off``, ``dry-run`` or ``write``). A cog reads
the flags at the start of a run, so a change takes effect on the next run
with no settings sync. Flags are written from the API's own protected
route, not through this client.

    GET /v1/flags   every flag (public)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FeatureFlagItem(BaseModel):
    """One feature flag, as ``GET /v1/flags`` lists it."""

    name: str = Field(..., description="The flag's unique name.")
    enabled: bool = Field(..., description="Whether the flag is on.")
    value: str | None = Field(
        default=None,
        description="The flag's optional value; None for a plain on/off flag.",
    )
    description: str | None = Field(default=None, description="What the flag switches.")
