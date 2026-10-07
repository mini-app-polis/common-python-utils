"""Discord webhook transport — channels, rate limits, and never raising.

Why this exists. api-kaianolevine-com built this for itself: a handful of
named channels, each resolved to its own webhook, a cooldown that stops a
rate-limited service posting through the limit, and a send that logs and
reports its failures rather than raising them. A second service wants the
same thing, and two copies of the cooldown logic is how one of them ends up
without the fix the other got. So it lives here, and each service keeps only
what is genuinely its own (api-kaianolevine-com's GitHub forwarding is built
on ``post_webhook`` below).

**Channels.** Every send names a channel, and this module resolves it to
that channel's own ``DISCORD_WEBHOOK_URL_*`` variable, falling back to
``DISCORD_WEBHOOK_URL`` when it is unset or blank. The fallback is the whole
migration strategy: a channel that exists in code but not yet in
configuration delivers to the original webhook instead of vanishing, so code
and Discord do not have to change in the same deploy. Callers pick a
channel; nothing here decides one for them, because the knowledge of what a
message *is* lives at the call site.

Four channels, and deliberately not more. The split is by what the reader is
doing when they look: something is broken (``errors``), something changed
(``activity``), the fleet ran (``runs``), or none of the above
(``default``). An unknown channel name has no variable of its own and takes
the fallback, which makes a typo indistinguishable from an unsplit channel —
the reason every send logs the channel it resolved.

**Configuration** comes from the process environment by default — the
fleet's Doppler → Railway convention — so a service needs no settings field
to use this. ``source`` replaces the environment with any mapping of
variable name to value: a test's fixed URLs, or a service whose own settings
class also reads a ``.env`` file and must keep resolving from it.

**Rate limits hold every send, not just the one refused.** A 429 starts a
cooldown for its scope — one webhook when Discord says the limit is that
webhook's bucket, all of them when it is global or when Cloudflare refused
the request before Discord saw it (error 1015, an HTML page rather than
JSON, applied to the caller's IP). Until the cooldown ends nothing is posted
to that scope: posting through a 1015 is what extends it. The limit is
reported to Sentry once, when it starts; the dropped sends are logged as
warnings. Cooldowns are process state, shared by every caller in the
process, because the limit Discord applies is too.

**Environment labeling.** ``send_message`` prefixes its content and embed
titles with ``[DEVELOPMENT]`` (or ``[LOCAL]``) outside production — the same
prefix ``pipeline_status`` puts on cog run reports, so every labeled message
in a channel reads the same way. Production is unmarked: a tag on every
production message trains the eye to skip it. ``send_payload`` posts a body
exactly as given, for producers that label it themselves or carry the
environment elsewhere.

**Mentions are off.** ``send_message`` sends ``allowed_mentions`` that
pings nobody unless told otherwise, because what it carries is often text
someone typed into a form, and an ``@everyone`` in a routine name must not
ping a channel. ``send_payload`` sends what it is given.

**Failures are logged and reported to Sentry, never raised.** Every
function returns whether Discord accepted the message. A dropped
notification is not a failed request or a failed job, and a caller that
thinks otherwise can act on the boolean. Sentry is reported to only when
``sentry_sdk`` is installed and initialised; this library does not depend
on it.

httpx logs each request's URL at INFO, and a webhook URL carries its token
in the path. ``mini_app_polis.logger`` redacts it from those lines; nothing
here configures logging.

Usage::

    from mini_app_polis import discord

    await discord.send_message(
        discord.CHANNEL_DEFAULT, content="Song added", context="songs/added"
    )
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import httpx

from . import _sentry
from .environment import Environment, current_environment
from .logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    LOG_WARNING,
    with_log_prefix,
)

#: A child of the package logger, not ``get_logger()``'s instance, so
#: records name this module and a test that stubs the shared logger
#: module does not swap this one out.
logger = logging.getLogger(__name__)

#: Channel names. Each maps to a ``DISCORD_WEBHOOK_URL_*`` variable below.
CHANNEL_DEFAULT = "default"
#: Anything that means something is broken, whatever produced it: failed CI,
#: dead-letter alarms, a service's own 5xx and machine-facing 4xx, a failed
#: background job.
CHANNEL_ERRORS = "errors"
#: The running list of committed data changes. Highest volume and lowest
#: per-message value — read by scrolling back, not by watching.
CHANNEL_ACTIVITY = "activity"
#: Cog run reports. Every severity, so the channel is a complete record of
#: the fleet's runs.
CHANNEL_RUNS = "runs"

#: The variable every channel falls back to.
FALLBACK_ENV = "DISCORD_WEBHOOK_URL"

#: Channel name -> the variable holding that channel's webhook. One module
#: defines both the set of channels and where each one's URL comes from.
CHANNEL_ENV: Mapping[str, str] = {
    CHANNEL_DEFAULT: "DISCORD_WEBHOOK_URL_DEFAULT",
    CHANNEL_ERRORS: "DISCORD_WEBHOOK_URL_ERRORS",
    CHANNEL_ACTIVITY: "DISCORD_WEBHOOK_URL_ACTIVITY",
    CHANNEL_RUNS: "DISCORD_WEBHOOK_URL_RUNS",
}

#: Suffix Discord exposes for GitHub-shaped payloads. Stripped from a
#: configured URL, so a value pasted the way GitHub's docs hand it over
#: still works for ordinary messages.
GITHUB_SUFFIX = "/github"

#: ``allowed_mentions`` that pings nobody. ``build_message``'s default: its
#: content is often text people typed — a dancer's name, a routine name — and
#: an ``@everyone`` in one must stay text rather than ping the channel.
NO_MENTIONS: Mapping[str, Any] = {"parse": []}

#: Seconds before a post is abandoned.
DEFAULT_TIMEOUT_SECS = 10.0

#: Cooldown key for a limit on every webhook at once.
_ALL_WEBHOOKS = "*"
#: When a 429 names no wait. Cloudflare's 1015 page usually does not.
_DEFAULT_COOLDOWN_SECS = 60.0
#: Upper bound on a cooldown, so a malformed Retry-After cannot mute Discord
#: for the rest of the process's life.
_MAX_COOLDOWN_SECS = 3600.0
#: A rejection body is logged for its reason; a 1015 is several kilobytes of
#: HTML, and the reason is in the first few hundred characters.
_BODY_LOG_LIMIT = 500

#: Cooldown scope (a webhook URL, or ``_ALL_WEBHOOKS``) -> the
#: ``time.monotonic()`` value before which nothing is posted to it.
_cooldowns: dict[str, float] = {}


def reset_cooldowns() -> None:
    """Forget every cooldown. For tests: one test's 429 must not mute the next."""
    _cooldowns.clear()


def _cooldown_remaining(url: str) -> float:
    """Seconds until ``url`` may be posted to again; zero or less means now."""
    deadline = max(_cooldowns.get(url, 0.0), _cooldowns.get(_ALL_WEBHOOKS, 0.0))
    return deadline - time.monotonic()


def _start_cooldown(url: str, resp: httpx.Response) -> tuple[float, str]:
    """Record the cooldown a 429 asks for; return its length and scope.

    Discord's own 429 is JSON with ``retry_after`` in seconds and ``global``
    saying whether it covers every route. Anything else — Cloudflare's HTML
    1015 page among them — is treated as global, with the ``Retry-After``
    header if there is one and the default if not.
    """
    secs: float | None = None
    scope = _ALL_WEBHOOKS
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        retry_after = data.get("retry_after")
        if isinstance(retry_after, int | float):
            secs = float(retry_after)
        if data.get("global") is False:
            scope = url
    if secs is None:
        try:
            secs = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            secs = None
    if not secs or secs <= 0:
        secs = _DEFAULT_COOLDOWN_SECS
    secs = min(secs, _MAX_COOLDOWN_SECS)
    _cooldowns[scope] = time.monotonic() + secs
    return secs, scope


def environment_prefix() -> str:
    """``"[DEVELOPMENT] "`` outside production, empty string inside it.

    Matches the prefix ``mini_app_polis.pipeline_status`` puts on cog run
    reports.
    """
    env = current_environment()
    if env is Environment.PRODUCTION:
        return ""
    return f"[{env.value.upper()}] "


def webhook_url(
    channel: str = CHANNEL_DEFAULT,
    *,
    source: Mapping[str, str | None] | None = None,
) -> str | None:
    """This channel's webhook URL with any ``/github`` suffix removed, or None.

    Reads the channel's own variable and falls back to
    ``DISCORD_WEBHOOK_URL`` when it is unset or blank. ``source`` replaces
    ``os.environ`` as the place those variables are read from.
    """
    values: Mapping[str, str | None] = os.environ if source is None else source
    name = CHANNEL_ENV.get(channel)
    raw = (values.get(name) or "").strip() if name else ""
    if not raw:
        raw = (values.get(FALLBACK_ENV) or "").strip()
    raw = raw.rstrip("/")
    if not raw:
        return None
    if raw.endswith(GITHUB_SUFFIX):
        raw = raw[: -len(GITHUB_SUFFIX)]
    return raw


def _client(timeout: float) -> httpx.AsyncClient:
    """One client per post. Here so a test can substitute a mock transport."""
    return httpx.AsyncClient(timeout=timeout)


async def post_webhook(
    url: str,
    *,
    json: dict[str, Any] | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    channel: str,
    context: str,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> bool:
    """POST to one webhook URL with cooldown handling; return whether accepted.

    The lowest level here, for a payload shape this module does not build —
    a GitHub event forwarded byte-for-byte to the ``/github`` suffix, say.
    ``channel`` and ``context`` name the destination and the producer in the
    log line only: a failure that says merely "notify" cannot be traced back
    to which caller it was. Never raises.
    """
    remaining = _cooldown_remaining(url)
    if remaining > 0:
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"discord rate-limited; not sending ({context}) channel={channel} "
                f"for another {remaining:.0f}s",
            )
        )
        return False

    try:
        async with _client(timeout) as client:
            resp = await client.post(url, content=content, json=json, headers=headers)
    except Exception as exc:  # httpx.HTTPError, and anything else: never raise
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"discord post failed ({context}) channel={channel}: {exc!r}",
            )
        )
        _sentry.capture_exception(exc)
        return False

    if resp.is_success:
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"discord notified ({context}) channel={channel} "
                f"status={resp.status_code}",
            )
        )
        return True

    if resp.status_code == 429:
        secs, scope = _start_cooldown(url, resp)
        held = "every webhook" if scope == _ALL_WEBHOOKS else "this webhook"
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"discord rate-limited ({context}) channel={channel}; "
                f"holding {held} for {secs:.0f}s",
            )
        )
        _sentry.capture_message(
            f"Discord rate limit ({context}) channel={channel}: holding {held} "
            f"for {secs:.0f}s",
            level="error",
        )
        return False

    # A non-2xx is Discord rejecting the message, not a transport fault: the
    # body says why, since the usual causes are a malformed embed or a revoked
    # webhook. Its start is enough for that.
    logger.error(
        with_log_prefix(
            LOG_FAILURE,
            f"discord rejected ({context}) channel={channel} "
            f"status={resp.status_code} body={resp.text[:_BODY_LOG_LIMIT]}",
        )
    )
    _sentry.capture_message(
        f"Discord rejected notification ({context}) channel={channel}: "
        f"{resp.status_code}",
        level="error",
    )
    return False


async def send_payload(
    channel: str,
    payload: dict[str, Any],
    *,
    context: str = "notify",
    source: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> bool:
    """Post a Discord message body, exactly as given, to this channel.

    ``payload`` is Discord's own shape (``content``, ``embeds``, …). Nothing
    is added to it, environment label included. Never raises.
    """
    try:
        base = webhook_url(channel, source=source)
    except Exception:  # a misbehaving source is still not the caller's failure
        logger.exception("discord webhook resolution failed (%s)", context)
        return False
    if base is None:
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                "no discord webhook resolved; dropping notification "
                f"channel={channel} context={context}",
            )
        )
        return False
    return await post_webhook(
        base, json=payload, channel=channel, context=context, timeout=timeout
    )


def build_message(
    *,
    content: str | None = None,
    embeds: Sequence[Mapping[str, Any]] | None = None,
    username: str | None = None,
    label: bool = True,
    allowed_mentions: Mapping[str, Any] | None = NO_MENTIONS,
) -> dict[str, Any]:
    """The Discord message body ``send_message`` would post.

    With ``label``, content and every embed title carry
    ``environment_prefix()``. Embeds are copied, never mutated.
    ``allowed_mentions`` defaults to pinging nobody (``NO_MENTIONS``); pass a
    mapping in Discord's shape to allow some, or None to leave the field
    out and take Discord's default, which parses every mention in content.
    """
    prefix = environment_prefix() if label else ""
    payload: dict[str, Any] = {}
    if content is not None:
        payload["content"] = f"{prefix}{content}"
    if embeds:
        rendered: list[dict[str, Any]] = []
        for embed in embeds:
            copy = dict(embed)
            if prefix and copy.get("title"):
                copy["title"] = f"{prefix}{copy['title']}"
            rendered.append(copy)
        payload["embeds"] = rendered
    if username:
        payload["username"] = username
    if allowed_mentions is not None:
        # Deep-copied: the default is shared, and a payload is the caller's.
        payload["allowed_mentions"] = deepcopy(dict(allowed_mentions))
    return payload


async def send_message(
    channel: str = CHANNEL_DEFAULT,
    *,
    content: str | None = None,
    embeds: Sequence[Mapping[str, Any]] | None = None,
    username: str | None = None,
    context: str = "notify",
    label: bool = True,
    allowed_mentions: Mapping[str, Any] | None = NO_MENTIONS,
    source: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> bool:
    """Post one message to this channel; return whether Discord accepted it.

    ``context`` names the producer in the log line. ``label=False`` leaves
    the environment prefix off; ``allowed_mentions`` is ``build_message``'s.
    Never raises — a message that cannot be built (an embed that is not a
    mapping) is logged and dropped.
    """
    if content is None and not embeds:
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"discord message has no content or embeds; not sending "
                f"channel={channel} context={context}",
            )
        )
        return False
    try:
        payload = build_message(
            content=content,
            embeds=embeds,
            username=username,
            label=label,
            allowed_mentions=allowed_mentions,
        )
    except Exception as exc:  # a malformed message is not the caller's failure
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"discord message could not be built ({context}) "
                f"channel={channel}: {exc!r}",
            )
        )
        _sentry.capture_exception(exc)
        return False
    return await send_payload(
        channel, payload, context=context, source=source, timeout=timeout
    )
