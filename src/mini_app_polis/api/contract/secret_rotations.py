"""Contract for secret rotations: what was rotated, and asking for one.

The secrets rotator mints a new value for one secret in one Doppler config,
keeps the old value valid beside it for a while (its ``_PREVIOUS`` slot), and
then retires the old one. It runs on a schedule and on demand. Each rotation
is recorded here as a rotation event, and these events are the only record:
the daily sweep reads the latest event per secret and config to decide what
is due.

    POST /v1/secrets/rotation-events   record one rotation, or update it
    GET  /v1/secrets/rotation-events   events, newest first, by secret and config
    POST /v1/secrets/rotations         ask for one secret to be rotated now

An event is keyed on its secret, its config and when the new value was
minted. Writing the same key again updates that event: the rotator writes it
once the new value has been verified (or has failed verification), and again
with ``retired`` once the old value is gone.

A rotation succeeds only when its new value is verified. Writing the value to
Doppler is not success, so an event always carries the verification result,
and one that failed says so.

**No model here carries a secret value.** Names, configs, kinds, people and
times only. Every string is either one of a fixed set, shaped by a pattern a
value does not fit, or bounded, and the request models refuse fields they do
not declare. The rotator never holds a value in a field of these models; a
value that reached one would be a bug in the rotator, and the shapes here
make most such bugs a 422 rather than a stored secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: How a secret is minted and checked, as the secrets inventory names it.
#: Secrets the inventory lists as never rotated have no events, so their
#: kind is not here.
RotationKind = Literal["machine-key", "generated", "aws-access-key", "manual"]

#: Why a rotation ran. ``routine``: it was due, and the old value stays valid
#: beside the new one until it is retired. ``compromised``: the old value may
#: be known to someone else, so it is retired at once.
RotationMode = Literal["routine", "compromised"]

#: The Doppler config the secret was rotated in.
RotationConfig = Literal["prd", "dev"]

#: Whether the new value was verified. Only ``ok`` is a successful rotation.
RotationVerification = Literal["ok", "failed"]

#: A secret's name in the inventory and in Doppler: an environment variable
#: name, never free text.
_NAME = r"^[A-Z][A-Z0-9_]*$"

#: Who rotated or asked: a machine by its name (``rotator-cog``:
#: lowercase words joined by hyphens, each starting with a letter) or a person
#: by email.
_ACTOR = (
    r"^(?:[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})$"
)

#: The last four characters of an AWS access key id. The id is not secret;
#: its secret key is never recorded.
_AWS_KEY_LAST4 = r"^[A-Z0-9]{4}$"

_EVENT_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class _RotationEventFields(BaseModel):
    """What is recorded about one rotation of one secret in one config."""

    #: The secret's inventory name.
    name: str = Field(..., min_length=1, max_length=128, pattern=_NAME)
    config: RotationConfig
    kind: RotationKind
    mode: RotationMode
    #: Who rotated it: the rotator for a scheduled rotation, or the person
    #: who asked for an on-demand one.
    actor: str = Field(..., min_length=1, max_length=254, pattern=_ACTOR)
    #: When the new value was minted.
    minted: datetime
    #: When the old value was retired; None while both are valid.
    retired: datetime | None = None
    verification: RotationVerification
    #: Why verification failed, in a sentence. Never the value, nor output
    #: that could quote it.
    verification_reason: str | None = Field(default=None, max_length=500)
    #: For an AWS access key, the last four characters of the new key's id.
    aws_key_last4: str | None = Field(default=None, pattern=_AWS_KEY_LAST4)


class RotationEventCreate(_RotationEventFields):
    """One rotation event, as the rotator writes it.

    Keyed on ``name``, ``config`` and ``minted``: writing the same key again
    updates the event.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _consistent(self) -> RotationEventCreate:
        if self.verification == "ok" and self.verification_reason is not None:
            raise ValueError("a verified rotation has no failure reason")
        if self.aws_key_last4 is not None and self.kind != "aws-access-key":
            raise ValueError(f"a {self.kind} rotation has no AWS key id")
        if self.retired is not None and self.retired < self.minted:
            raise ValueError("the old value is retired after the new one is minted")
        return self


class RotationEvent(_RotationEventFields):
    """A stored rotation event. Tolerates fields a newer API adds."""

    id: str = Field(..., pattern=_EVENT_ID)
    #: When the API last wrote it.
    recorded_at: datetime


class RotationRequest(BaseModel):
    """Ask for one secret to be rotated now, in one config.

    The API puts it on the rotator's queue; the rotation is recorded as an
    event once it has run. The person asking is the caller, so it is not
    sent.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, pattern=_NAME)
    config: RotationConfig
    mode: RotationMode


class RotationRequestAccepted(BaseModel):
    """The request is on the rotator's queue."""

    accepted: bool = True
    message_id: str = Field(..., max_length=100)
    name: str = Field(..., max_length=128, pattern=_NAME)
    config: RotationConfig
