"""Contract for ``POST /v1/notify``, the fleet's Discord path.

Used by :mod:`mini_app_polis.pipeline_status` on behalf of every cog.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

#: ``reason`` when the message went to Discord straight away (``forwarded``
#: is true).
NOTIFY_FORWARDED = "forwarded"

#: ``reason`` when the message could not go out within the API's short wait
#: and was accepted into its outbox instead. ``forwarded`` is false, but the
#: message will be sent: a caller treats it as accepted, not as a failure.
NOTIFY_QUEUED = "queued"

#: ``reason`` when Discord refused the message outright (a 4xx other than a
#: rate limit, such as a malformed embed). ``forwarded`` is false and the
#: message is not retried.
NOTIFY_DELIVERY_FAILED = "delivery_failed"


class NotificationResult(BaseModel):
    """What the notification routes decided about one inbound event.

    Returned for drops as well as sends. GitHub gets a 200 either way, so the
    body is the only place the reason is visible to whoever is reading a
    delivery in GitHub's UI.

    ``POST /v1/notify`` answers 200 with this body for every outcome of a
    Discord send; none of them is a 5xx. ``reason`` is one of
    :data:`NOTIFY_FORWARDED`, :data:`NOTIFY_QUEUED` or
    :data:`NOTIFY_DELIVERY_FAILED` in production, and
    ``development_not_reported`` when the development API declines to post an
    unmarked message. It stays a plain string so that a token the API adds
    later is read rather than rejected.
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
        ...,
        description=(
            "Why it was forwarded, queued or dropped, in one token. 'queued' "
            "means accepted for later delivery, not a failure."
        ),
    )


class NotifyRequest(BaseModel):
    """An ad-hoc Discord message from first-party code.

    Deliberately Discord's own message shape rather than an abstraction over
    it. A wrapper would have to grow a field every time a caller wanted one
    Discord already has, and the callers are all in this ecosystem — there is
    no second transport to stay portable for.

    ``contract_suite_result`` is the one field that is not Discord's. It is
    read by the API and never sent on to Discord.
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
    contract_suite_result: bool = Field(
        default=False,
        description=(
            "Marks the message as the contract suite's end-of-run result. The "
            "development API forwards a message only when the contract-suite "
            "machine sends it with this mark; everything else from development "
            "is dropped."
        ),
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
