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
    GET  /v1/secrets/changes           names Doppler reported changed, oldest first

    POST /v1/secrets/rotation-incidents                       start or resume an incident
    POST /v1/secrets/rotation-incidents/{id}/entries          register its entries
    POST /v1/secrets/rotation-incidents/{id}/task-requested   mark an entry's task asked for
    GET  /v1/secrets/rotation-incidents/{id}                  each entry's state

An incident is a compromised rotation of every secret in the inventory, in
both configs, declared at once and tracked under one id. The API mints the
id and queues one ``rotation.incident`` message; the rotator registers the
incident's entries (the inventory as it stood when the incident was
declared), queues a compromised ``rotation.job`` for each secret it can
rotate, and asks for a board task for each one it cannot. Every job, event
and task of the incident carries its id. An entry's state is derived from
the rotation events minted after the incident started and from whether its
task was asked for, in this precedence: ``rotated``, ``failed``,
``task-opened``, ``outstanding``.

An event is keyed on its secret, its config and when the new value was
minted. Writing the same key again updates that event: the rotator writes it
once the new value has been verified (or has failed verification), and again
with ``retired`` once the old value is gone.

An event always carries what is known about the new value. ``ok``: a check
proved it works. ``failed``: the check did not pass, or did not run when one
exists, and the event says why. ``unchecked``: the value was written through
the rotation process and no automatic check exists for that secret. An
unchecked rotation counts as rotated; a broken value shows up in the
failures of whatever uses it.

**No model here carries a secret value.** Names, configs, kinds, people and
times only. Every string is either one of a fixed set, shaped by a pattern a
value does not fit, or bounded, and the request models refuse fields they do
not declare. The rotator never holds a value in a field of these models; a
value that reached one would be a bug in the rotator, and the shapes here
make most such bugs a 422 rather than a stored secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

#: How a secret is minted and checked, as the secrets inventory names it.
#: ``provider-api``: minted by the provider, through its API, by the rotator
#: (a ``manual`` secret is minted in the provider's console by a person).
#: Secrets the inventory lists as never rotated have no events, so their
#: kind is not here.
RotationKind = Literal[
    "machine-key", "generated", "aws-access-key", "manual", "provider-api"
]

#: Why a rotation ran. ``routine``: it was due, and the old value stays valid
#: beside the new one until it is retired. ``compromised``: the old value may
#: be known to someone else, so it is retired at once.
RotationMode = Literal["routine", "compromised"]

#: The Doppler config the secret was rotated in.
RotationConfig = Literal["prd", "dev"]

#: What is known about the new value. ``ok``: a check proved it works.
#: ``failed``: it did not. ``unchecked``: written through the rotation
#: process, and no automatic check exists for the secret. ``ok`` and
#: ``unchecked`` both count as rotated.
RotationVerification = Literal["ok", "failed", "unchecked"]

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

#: An incident's id, minted by the API: a lowercase UUID.
_INCIDENT_ID = _EVENT_ID

#: A secret's tier in the inventory: 0 is rotated first in an incident.
RotationTier = Literal[0, 1, 2, 3]

#: How an incident deals with one entry. ``worker``: the rotator rotates it
#: with a compromised ``rotation.job``. ``task``: a person does, from a
#: board task (a manual kind, an entry with steps, or one never rotated).
RotationIncidentHandling = Literal["worker", "task"]

#: Where one entry of an incident stands. ``rotated``: a verified rotation
#: (``ok`` or ``unchecked``) was minted after the incident started.
#: ``failed``: the latest rotation minted since then failed.
#: ``task-opened``: its board task was asked for and it is not rotated.
#: ``outstanding``: none of those yet. When several hold, the first in that
#: order is the state.
RotationIncidentState = Literal["rotated", "failed", "task-opened", "outstanding"]


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
    #: The incident the rotation was part of; None for a scheduled or
    #: single on-demand rotation.
    incident_id: str | None = Field(default=None, pattern=_INCIDENT_ID)


class RotationEventCreate(_RotationEventFields):
    """One rotation event, as the rotator writes it.

    Keyed on ``name``, ``config`` and ``minted``: writing the same key again
    updates the event.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _consistent(self) -> RotationEventCreate:
        if self.verification != "failed" and self.verification_reason is not None:
            raise ValueError("a rotation that did not fail has no failure reason")
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
    #: The incident this job belongs to. Set by the rotator on the jobs it
    #: queues for an incident, which are always rotated in compromised mode.
    #: Left out of the serialized form when unset, so a consumer on a release
    #: from before incidents (which forbids the field) still reads the job.
    incident_id: str | None = Field(default=None, pattern=_INCIDENT_ID)

    @model_serializer(mode="wrap")
    def _omit_no_incident(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("incident_id") is None:
            data.pop("incident_id", None)
        return data


class RotationRequestAccepted(BaseModel):
    """The request is on the rotator's queue."""

    accepted: bool = True
    message_id: str = Field(..., max_length=100)
    name: str = Field(..., max_length=128, pattern=_NAME)
    config: RotationConfig


#: What Doppler reported happened to a secret's name in a config.
SecretChangeKind = Literal["added", "removed", "updated"]


class SecretChange(BaseModel):
    """One secret name Doppler reported changed in one config.

    Doppler's ``config.secrets.update`` webhook lists the names added,
    removed and updated, never values; the API stores one change per name.
    The rotator's sweep compares changes with rotation events, so a value
    changed outside the rotation process is found. Tolerates fields a newer
    API adds.
    """

    name: str = Field(..., min_length=1, max_length=128, pattern=_NAME)
    config: RotationConfig
    change: SecretChangeKind
    #: When the API received Doppler's webhook.
    changed_at: datetime
    #: The stored webhook delivery the change came from; several names share
    #: one when they changed together.
    received_id: str = Field(..., max_length=36, pattern=_EVENT_ID)


class RotationIncidentRequest(BaseModel):
    """Start an incident: rotate every inventory secret, compromised, now.

    Without ``incident_id`` the API mints a new incident. With the id of one
    it already holds, it queues that incident again, and the rotator skips
    what is already done (resume). The person asking is the caller, so it
    is not sent.
    """

    model_config = ConfigDict(extra="forbid")

    #: Always ``compromised``: an incident retires every old value at once.
    mode: Literal["compromised"]
    incident_id: str | None = Field(default=None, pattern=_INCIDENT_ID)


class RotationIncidentAccepted(BaseModel):
    """The incident is on the rotator's queue."""

    accepted: bool = True
    message_id: str = Field(..., max_length=100)
    incident_id: str = Field(..., pattern=_INCIDENT_ID)
    #: When the incident was declared; rotations minted after it count.
    started_at: datetime
    #: True when this request queued an incident the API already held.
    resumed: bool = False


class RotationIncidentEntry(BaseModel):
    """One secret in one config, as the rotator registers it for an incident."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, pattern=_NAME)
    config: RotationConfig
    tier: RotationTier
    handling: RotationIncidentHandling


class RotationIncidentRegistration(BaseModel):
    """Every entry of an incident, registered once by the rotator.

    Registering again (a resumed incident) replaces the list but keeps what
    the API already recorded for an entry, such as its task being asked for.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[RotationIncidentEntry] = Field(..., min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _unique(self) -> RotationIncidentRegistration:
        keys = [(entry.name, entry.config) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("an entry is registered once per name and config")
        return self


class RotationIncidentTaskRequested(BaseModel):
    """The board task for one entry of an incident has been asked for."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, pattern=_NAME)
    config: RotationConfig


class RotationIncidentEntryStatus(BaseModel):
    """One registered entry and where it stands. Tolerates fields a newer
    API adds."""

    name: str = Field(..., max_length=128, pattern=_NAME)
    config: RotationConfig
    tier: RotationTier
    handling: RotationIncidentHandling
    task_requested: bool
    state: RotationIncidentState


class RotationIncident(BaseModel):
    """An incident and each of its entries. Tolerates fields a newer API
    adds.

    ``entries`` is every registered entry, in the order registered.
    ``outstanding`` is every entry not rotated: failed ones first, then prd
    before dev, then by tier. Empty ``entries`` means the rotator has not
    registered the incident yet.
    """

    incident_id: str = Field(..., pattern=_INCIDENT_ID)
    started_at: datetime
    entries: list[RotationIncidentEntryStatus]
    outstanding: list[RotationIncidentEntryStatus]
