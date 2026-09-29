"""Contract for ``POST /v1/notify``, the fleet's Discord path.

Used by :mod:`mini_app_polis.pipeline_status` on behalf of every cog.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class NotificationResult(BaseModel):
    """What the notification routes decided about one inbound event.

    Returned for drops as well as sends. GitHub gets a 200 either way, so the
    body is the only place the reason is visible to whoever is reading a
    delivery in GitHub's UI.
    """

    forwarded: bool = Field(
        ..., description="Whether the message reached the Discord webhook."
    )
    event: str = Field(..., description="GitHub event name, or 'notify'.")
    outcome: str | None = Field(
        default=None,
        description="Run conclusion or commit-status state, when the payload had one.",
    )
    reason: str = Field(
        ..., description="Why it was forwarded or dropped, in one token."
    )


class NotifyRequest(BaseModel):
    """An ad-hoc Discord message from first-party code.

    Deliberately Discord's own message shape rather than an abstraction over
    it. A wrapper would have to grow a field every time a caller wanted one
    Discord already has, and the callers are all in this ecosystem — there is
    no second transport to stay portable for.
    """

    content: str | None = Field(
        default=None,
        max_length=2000,
        description="Message text. Discord's own limit is 2000 characters.",
    )
    embeds: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=10,
        description="Discord embed objects, passed through unmodified.",
    )
    username: str | None = Field(
        default=None,
        max_length=80,
        description="Override the webhook's display name for this message.",
    )

    @model_validator(mode="after")
    def require_a_body(self) -> NotifyRequest:
        """Reject a message with nothing in it — Discord would 400 anyway."""
        if not self.content and not self.embeds:
            raise ValueError("one of content or embeds is required")
        return self

    def to_discord(self) -> dict[str, Any]:
        """Render the Discord webhook body, omitting anything unset."""
        payload: dict[str, Any] = {}
        if self.content:
            payload["content"] = self.content
        if self.embeds:
            payload["embeds"] = self.embeds
        if self.username:
            payload["username"] = self.username
        return payload
