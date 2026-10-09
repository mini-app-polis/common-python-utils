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

**Rate limits are waited out, within a budget.** Sends to one webhook go
out one at a time, in order. Discord says on every answer how many posts
its bucket has left and when it refills (``X-RateLimit-Remaining``,
``X-RateLimit-Reset-After``); at zero the next send waits for the refill
instead of earning a 429. A 429 starts a cooldown for its scope — one
webhook when Discord says the limit is that webhook's bucket, all of them
when it is global or when Cloudflare refused the request before Discord saw
it (error 1015, an HTML page rather than JSON, applied to the caller's IP)
— and the refused message is sent again when it ends. Nothing is posted to
a scope while it is cooling down: posting through a 1015 is what extends
it.

Waiting is bounded. A send spends at most ``max_wait`` seconds (default
``MAX_WAIT_SECS``) queued behind others and waiting out limits; a message
that would need longer is dropped and logged — a burst larger than the
bucket loses its tail, a 1015's minute-long hold loses what is sent during
it — rather than holding its caller indefinitely. The first drop of an
episode is reported to Sentry; the next successful post to that webhook
ends the episode. Cooldowns, buckets and the queue are process state,
shared by every caller in the process, because the limit Discord applies
is too.

Before 2026-10, a 429 dropped everything sent until its cooldown ended. A
bucket's cooldown is a fraction of a second, so an evaluator pass posting a
dozen messages a second lost most of them, each refusal a Sentry event.

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

import asyncio
import logging
import os
import time
import weakref
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

#: Seconds a send may spend queued behind other sends to its webhook and
#: waiting out rate limits, before it is dropped. Long enough for a burst to
#: drain at Discord's per-webhook rate (about five posts per two seconds);
#: short enough that a caller awaiting the send is not held for a 1015.
MAX_WAIT_SECS = 10.0

#: Attempts at one message, the first included: a 429 is retried after its
#: cooldown, but a webhook that keeps refusing is not argued with.
_MAX_ATTEMPTS = 3

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
#: ``time.monotonic()`` value before which nothing is posted to it. An
#: emptied bucket is recorded here too, as a cooldown for its webhook.
_cooldowns: dict[str, float] = {}

#: Webhook URLs whose current rate-limit episode has been reported to
#: Sentry. Cleared for a URL by its next successful post.
_reported_drops: set[str] = set()

#: Event loop -> webhook URL -> the lock that sends to it one at a time.
#: Per loop, because an asyncio lock belongs to the loop it first waited
#: on, and a test runner (or a sync caller using ``asyncio.run``) makes
#: many loops in one process.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]
_locks = weakref.WeakKeyDictionary()


def reset_cooldowns() -> None:
    """Forget every cooldown and episode. For tests: one test's 429 must not
    mute the next."""
    _cooldowns.clear()
    _reported_drops.clear()


def _lock(url: str) -> asyncio.Lock:
    """This loop's lock for ``url``."""
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    lock = per_loop.get(url)
    if lock is None:
        lock = per_loop[url] = asyncio.Lock()
    return lock


def _now() -> float:
    """``time.monotonic()``. Here so a test can run the clock instead of waiting."""
    return time.monotonic()


async def _sleep(secs: float) -> None:
    """``asyncio.sleep``. Here so a test can run the clock instead of waiting."""
    await asyncio.sleep(secs)


def _cooldown_remaining(url: str) -> float:
    """Seconds until ``url`` may be posted to again; zero or less means now."""
    deadline = max(_cooldowns.get(url, 0.0), _cooldowns.get(_ALL_WEBHOOKS, 0.0))
    return deadline - _now()


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
    _cooldowns[scope] = max(_cooldowns.get(scope, 0.0), _now() + secs)
    return secs, scope


def _note_bucket(url: str, resp: httpx.Response) -> None:
    """Hold ``url`` until its bucket refills, if this answer emptied it.

    Discord reports the bucket on every answer. Waiting for the refill here
    is what keeps a burst from being answered with 429s at all.
    """
    if resp.headers.get("X-RateLimit-Remaining") != "0":
        return
    try:
        secs = float(resp.headers.get("X-RateLimit-Reset-After", ""))
    except ValueError:
        return
    if secs <= 0:
        return
    secs = min(secs, _MAX_COOLDOWN_SECS)
    _cooldowns[url] = max(_cooldowns.get(url, 0.0), _now() + secs)


def _drop(url: str, channel: str, context: str, why: str) -> bool:
    """Log a send given up on for a rate limit; report the episode once."""
    logger.warning(
        with_log_prefix(
            LOG_WARNING,
            f"discord rate-limited; dropping ({context}) channel={channel}: {why}",
        )
    )
    if url not in _reported_drops:
        _reported_drops.add(url)
        _sentry.capture_message(
            f"Discord rate limit is dropping notifications ({context}) "
            f"channel={channel}: {why}",
            level="error",
        )
    return False


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
    max_wait: float = MAX_WAIT_SECS,
) -> bool:
    """POST to one webhook URL, waiting out rate limits; return whether accepted.

    The lowest level here, for a payload shape this module does not build —
    a GitHub event forwarded byte-for-byte to the ``/github`` suffix, say.
    ``channel`` and ``context`` name the destination and the producer in the
    log line only: a failure that says merely "notify" cannot be traced back
    to which caller it was. ``max_wait`` bounds the time spent queued and
    waiting on limits, not the request itself (``timeout``). Never raises.
    """
    deadline = _now() + max_wait
    lock = _lock(url)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=max(max_wait, 0.0))
    except TimeoutError:
        return _drop(url, channel, context, f"still queued after {max_wait:.0f}s")
    try:
        return await _post_in_turn(
            url,
            json=json,
            content=content,
            headers=headers,
            channel=channel,
            context=context,
            timeout=timeout,
            deadline=deadline,
        )
    finally:
        lock.release()


async def _post_in_turn(
    url: str,
    *,
    json: dict[str, Any] | None,
    content: bytes | None,
    headers: dict[str, str] | None,
    channel: str,
    context: str,
    timeout: float,
    deadline: float,
) -> bool:
    """``post_webhook`` once it holds the webhook's lock."""
    for _attempt in range(_MAX_ATTEMPTS):
        wait = _cooldown_remaining(url)
        if wait > 0:
            if _now() + wait > deadline:
                return _drop(url, channel, context, f"held for another {wait:.0f}s")
            await _sleep(wait)

        try:
            async with _client(timeout) as client:
                resp = await client.post(
                    url, content=content, json=json, headers=headers
                )
        except Exception as exc:  # httpx.HTTPError, and anything else: never raise
            logger.error(
                with_log_prefix(
                    LOG_FAILURE,
                    f"discord post failed ({context}) channel={channel}: {exc!r}",
                )
            )
            _sentry.capture_exception(exc)
            return False

        _note_bucket(url, resp)

        if resp.is_success:
            _reported_drops.discard(url)
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
            # A warning, not a fault: waiting it out is the plan. A message
            # that cannot be is reported by _drop.
            logger.warning(
                with_log_prefix(
                    LOG_WARNING,
                    f"discord rate-limited ({context}) channel={channel}; "
                    f"holding {held} for {secs:.1f}s",
                )
            )
            continue

        # A non-2xx is Discord rejecting the message, not a transport fault:
        # the body says why, since the usual causes are a malformed embed or
        # a revoked webhook. Its start is enough for that.
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

    return _drop(url, channel, context, f"refused {_MAX_ATTEMPTS} times")


async def send_payload(
    channel: str,
    payload: dict[str, Any],
    *,
    context: str = "notify",
    source: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
    max_wait: float = MAX_WAIT_SECS,
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
        base,
        json=payload,
        channel=channel,
        context=context,
        timeout=timeout,
        max_wait=max_wait,
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
    max_wait: float = MAX_WAIT_SECS,
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
        channel,
        payload,
        context=context,
        source=source,
        timeout=timeout,
        max_wait=max_wait,
    )
