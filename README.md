# common-python-utils

Common Python utilities shared across Kaiano's projects.

> **Install name:** `miniapppolis-common-utils` (PyPI) &nbsp;·&nbsp; **Import namespace:** `mini_app_polis`
>
> The distribution and the import namespace differ on purpose: the published
> name is prefixed because the registry is public, while the import namespace
> is the one every consumer already writes. Install `miniapppolis-common-utils`,
> import `mini_app_polis` (for example, `from mini_app_polis.google import GoogleAPI`).

---

## Installation

Declare a version range in your `pyproject.toml` and let the lockfile carry
the exact pin:

```toml
[project]
dependencies = [
  "miniapppolis-common-utils>=6.0,<7",
]
```

Then `uv lock`. To move to a newer release, `uv lock --upgrade-package
miniapppolis-common-utils` — no edit to this file.

To use the LLM module, add the `llm` extra:

```toml
dependencies = [
  "miniapppolis-common-utils[llm]>=6.0,<7",
]
```

---

## Modules

| Module | Import | Description |
|--------|--------|-------------|
| `activity.py` | `from mini_app_polis import activity` | A FastAPI service's Discord feed: committed data changes, faults, announce-on-commit, background faults (`activity` extra) |
| `api/` | `from mini_app_polis.api import KaianoApiClient` | HTTP client for internal FastAPI services |
| `api/contract/` | `from mini_app_polis.api.contract import IngestSet, ENDPOINTS` | Typed request/response models for the Kaiano API endpoints the fleet calls |
| `asana/` | `from mini_app_polis.asana import AsanaClient` | Asana task creation with external-id idempotency |
| `config.py` | `from mini_app_polis import config` | Env-var driven shared config (Spotify, Google, VDJ) |
| `deadline.py` | `from mini_app_polis.deadline import run_deadline` | A run deadline the library's retry loops respect: no attempt starts, and no backoff is waited out, past it; `without_run_deadline()` lifts it for cleanup |
| `discord.py` | `from mini_app_polis import discord` | Discord webhook transport: named channels, rate-limit cooldowns, environment labels, never raises |
| `doppler.py` | `from mini_app_polis.doppler import DopplerClient` | Reads and writes secrets in one Doppler config (API or CLI), and the `check-doppler-keys` console script |
| `environment.py` | `from mini_app_polis.environment import current_environment, effect_enabled` | Which environment this process is in, which outbound effects it may make, and which name an environment-specific variable is read under (`_DEV` outside production) |
| `google/` | `from mini_app_polis.google import GoogleAPI` | Drive + Sheets facade |
| `lambda_worker.py` | `from mini_app_polis.lambda_worker import deadline, parse_message, process_records` | A Lambda queue consumer's glue: stop before the timeout (`RunOutOfTime`), read a `{type, version, payload}` message, name failed records in `batchItemFailures` |
| `llm/` | `from mini_app_polis.llm import build_llm, LLMMessage` | OpenAI + Anthropic clients (optional extra) |
| `logger.py` | `from mini_app_polis import logger` | The fleet's logger: level from `LOGGING_LEVEL`, JSON on Railway, credential-printing libraries held at INFO |
| `mp3/` | `from mini_app_polis.mp3 import ...` | AcoustID identification, tagging, renaming |
| `music/` | `from mini_app_polis.music import normalize_for_matching` | Music data normalization utilities |
| `pipeline_status.py` | `from mini_app_polis.pipeline_status import RunReport, post_findings` | Run reports to the runs channel and graded findings to the API |
| `request_metrics.py` | `from mini_app_polis.request_metrics import RequestMetricsMiddleware` | ASGI middleware publishing an API's request latency and error counts to CloudWatch |
| `sentry.py` | `from mini_app_polis.sentry import init_sentry` | Starts Sentry for a service with the fleet defaults; an empty DSN in production logs an ERROR instead of skipping silently (CD-038) |
| `spotify/` | `from mini_app_polis.spotify import SpotifyAPI` | Spotipy wrapper; raises `SpotifyTokenExpired` when Spotify refuses the refresh token (`invalid_grant`, not retried); every request, token refresh included, is bounded by `SPOTIFY_REQUEST_TIMEOUT_SECONDS` |
| `ssm_secrets.py` | `from mini_app_polis.ssm_secrets import load_secrets` | Loads a Lambda worker's secrets from SSM Parameter Store into the environment, at cold start and again each invocation |
| `timing.py` | `from mini_app_polis import timing` | Per-invocation working vs waiting time, by service, as one CloudWatch line |
| `vdj/` | `from mini_app_polis.vdj.m3u import ParseFacade` | VirtualDJ M3U parsing |

---

## Usage

### KaianoApiClient

```python
from mini_app_polis.api import KaianoApiClient

# Set KAIANO_API_BASE_URL (KAIANO_API_BASE_URL_DEV outside production)
# and this machine's own key (deejay-cog -> DEEJAY_COG_API_KEY) in the
# environment. The machine name is what selects the key variable, and the
# key is what names this caller to the API. Without it the client falls
# back to KAIANO_API_KEY; with neither set, every call raises
# KaianoApiError before any request is sent.
client = KaianoApiClient.from_env("deejay-cog")
result = client.post("/sets", {"name": "My Set"})
```

For the endpoints in `mini_app_polis.api.contract.ENDPOINTS`, use the typed
methods. They take the request model the API validates against and return the
envelope's `data` as the response model. `get()` and `post()` still work for
anything outside the catalog.

```python
from mini_app_polis.api.contract import TranscriptionRunRequest

accepted = client.request_transcription_run(
    TranscriptionRunRequest(mode="wcs-transcripts", drive_file_id=file_id)
)
if accepted.deduplicated:
    ...
```

The API imports these same models for its routes, so the contract has a
single definition. A response that doesn't match raises pydantic's
`ValidationError`, not `KaianoApiError`. Response models never forbid extra
fields, so a field the API adds doesn't break a cog pinned to an older
release. A test enforces this.

**Retries.** A GET is retried on a lost connection, on a gateway's 502, 503
or 504, and on 429. Each wait is longer than the last (about 2 s, then 4 s,
jittered), or as long as the response's `Retry-After`, and never more than
30 s. Retrying immediately is what turned one slow wiki export into three
concurrent builds of it. A POST is retried only on a lost connection. Inside
`run_deadline`, the client follows the same rules as the library's other
retry loops.

**Conditional reads.** `export_wcs_wiki()` keeps the last body it read and
sends its ETag back. An unchanged corpus comes back as an empty 304, and the
kept body is validated again, so each call gets a model of its own. A warm
Lambda keeps that copy between invocations. The export also gets a 120 s
timeout, since a cold build takes longer than the default 30 s.

**Contract suite.** `contract/` checks the catalog against the deployed
development API (TEST-016). It calls every endpoint through the typed methods
and fails if an endpoint is neither exercised nor listed in
`contract/harness.py`'s `NOT_EXERCISED`. It writes data, so it refuses to run
unless it has a `dev_` key and a non-production host, and it stops if the API
doesn't recognise the key as `contract-suite` (CD-033). It runs from
`.github/workflows/contract.yml` on the promotion pull request to `main`, where
it gates the release, and when the API's dev deploy dispatches
`dev-api-deployed`.
Locally:

```bash
CONTRACT_API_URL=https://dev-api.kaianolevine.com CONTRACT_SUITE_API_KEY=dev_… \
  uv run pytest contract --no-cov
```

### AsanaClient

```python
import os
from datetime import date

from mini_app_polis.asana import AsanaClient, AsanaTaskInput, link, rich_text_body

# Set ASANA_ACCESS_TOKEN (personal access token) and, if you resolve tag
# names, ASANA_WORKSPACE_ID in the environment.
client = AsanaClient.from_env()

external_id = "voicenote.1AbCdEf"

# Idempotency is a single lookup, not a list-and-scan: Asana stores an
# app-scoped `external` object on each task and lets you address the task
# by it. Completed tasks are found too, so a triaged item is never
# recreated.
if client.find_task_by_external_id(external_id) is None:
    client.create_task(
        AsanaTaskInput(
            name="Send the report",
            html_notes=rich_text_body(
                "Because it is due Friday.",
                link("https://example.com/source", "Source"),
            ),
            project_gid=os.environ["ASANA_PROJECT_GID"],
            section_gid=os.environ["ASANA_SECTION_GID"],
            assignee="me",
            due_on=date.today(),
            tag_gids=(client.find_or_create_tag("review"),),
            external_id=external_id,
        )
    )
```

Task bodies are Asana rich text, not markdown, and Asana returns 400 on
malformed markup. Compose them with `rich_text_body` / `link` /
`escape_rich_text` rather than by hand — transcripts and model output
contain `<` and `&` often enough that escaping is a correctness concern.

### LLM (requires `llm` extra)

```python
from mini_app_polis.llm import build_llm, LLMMessage

llm = build_llm(provider="anthropic", model="claude-3-5-sonnet-20241022")
result = llm.generate_json(
    messages=[
        LLMMessage(role="system", content="Return JSON only."),
        LLMMessage(role="user", content="Extract the artist and title."),
    ],
    json_schema={
        "type": "object",
        "properties": {
            "artist": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["artist", "title"],
    },
)
print(result.output_json)  # {"artist": "...", "title": "..."}
```

`generate_json` reads the first JSON object in the response: stray text
before or after it (a "Here is the JSON:" line, a trailing remark, a
markdown fence) is ignored. A response with no object fails with
`LLMValidationError` quoting its start. Schema validation is unchanged.

### Sentry

```python
from mini_app_polis.sentry import init_sentry

# A Lambda handler, at import (once per cold start). Reads SENTRY_DSN_COGS.
init_sentry("watcher-cog")

# A FastAPI app, in its lifespan, from its Settings.
sentry_on = init_sentry(
    "api-kaianolevine-com",
    dsn_var="SENTRY_DSN_APIS",
    dsn=settings.SENTRY_DSN_APIS,
    integrations=[FastApiIntegration()],
    traces_sample_rate=1.0,
)
```

The environment label comes from `mini_app_polis.environment`, and the
`service` tag tells events apart in a shared project. With no DSN in
production it logs an ERROR naming the variable and the service starts
anyway; outside production, no DSN is normal and it stays quiet. It never
raises, and returns whether Sentry is on.

### Discord

```python
from mini_app_polis import discord

await discord.send_message(
    discord.CHANNEL_ERRORS,
    content="Nightly export failed",
    context="export",  # names the producer in the log line
)
```

Four channels — `default`, `errors`, `activity`, `runs` — each read from its
own `DISCORD_WEBHOOK_URL_<CHANNEL>` and falling back to `DISCORD_WEBHOOK_URL`
when that is unset, so a channel can exist in code before it exists in
Discord. Outside production, content and embed titles are prefixed
`[DEVELOPMENT]`, and mentions are off (`allowed_mentions` pings nobody), so
text a person typed cannot ping `@everyone`; `send_payload` posts a body
exactly as given instead. Every
call returns whether Discord accepted the message and never raises: failures
are logged and, if the process has initialised `sentry_sdk`, reported there.
Sends to one webhook go out one at a time, in order, paced by the
bucket Discord reports on every answer; a 429 holds that webhook (or all of
them, for a global or Cloudflare limit) and the message is sent again when
the hold ends, rather than posting through it. No hold is longer than a
minute, whatever the 429 asks for: the next message after it finds out
whether the limit is still there, and a 429 logs what Discord said. A send waits at most
`MAX_WAIT_SECS` (10 s; `max_wait=` to change it) — a burst larger than that
loses its tail, a Cloudflare 1015 drops what is sent during it — and the
first drop of an episode is reported to Sentry. `webhook_url(..., source=...)` and the `source=` argument on the send
functions read the variables from a mapping instead of the environment.
`post_webhook` is the rate-limit-aware POST underneath, for payloads this
module does not shape (GitHub events forwarded to the webhook's `/github`
suffix). See the module docstring for the rest.

### Activity feed (requires `activity` extra)

Committed data changes to `activity`, faults to `errors`, for a FastAPI
service on SQLAlchemy 2. Needs the `activity` extra; every 6.x release has
these modules:

```toml
dependencies = ["miniapppolis-common-utils[activity]>=6.0,<7"]
```

```python
from mini_app_polis import activity, discord

config = activity.ActivityConfig(
    service="api-deejaytools",                 # shown in every footer
    suppressed_tables={"identity_audit_events"},
    excluded_paths=("/health", "/version"),
    # is_machine=lambda request: request.state.caller_kind == "machine",
)
# After the middleware whose status it should see, so it wraps them.
app.middleware("http")(activity.activity_middleware(config))
```

Importing the module registers the session listeners. One message per
request that committed something (rolled-back work reports nothing; bulk
statements are counted as statements), and one per 5xx, unhandled
exception, or — when `is_machine` is given — 4xx to a machine caller. The
actor is `request.state.caller` unless `caller=` says otherwise. A fault
names the exception's type and Sentry id, never its message. Titles carry
the environment prefix only with `ActivityConfig(label=True)` (and
`report_fault(..., label=True)`); the footer always names the environment. A handler, or
a pure ASGI error middleware holding only the scope, can attach a reason
with `activity.record_fault_detail(request_or_scope, "...")`.

Two producers for what a request tally cannot say:

```python
# Posted when this session's transaction commits; never on rollback.
# Works in a request and in a background job with its own session.
# `title` is user-typed: mentions are off, so an "@everyone" in it stays
# text, but escape Discord markdown in it yourself if that matters.
activity.announce_on_commit(
    session, discord.CHANNEL_ACTIVITY, content=f"Song added: {title}"
)
await session.commit()

# A scheduler tick or job runner with no request around it.
except Exception as exc:
    await activity.report_fault("scheduler · drive_jobs", exc, service="api-deejaytools")
```

Delivery is fire-and-forget; `await activity.wait_for_deliveries()` lets
tests (and a shutdown) wait for it. See the module docstring for what the
listeners cannot see.

### Run deadline

```python
from mini_app_polis.deadline import RunDeadlineReached, run_deadline

# A Lambda handler: stop 30 s before the hard ceiling.
budget_s = context.get_remaining_time_in_millis() / 1000 - 30
try:
    with run_deadline(budget_s):
        run()
except RunDeadlineReached:
    ...  # stop cleanly; report a partial run
```

Inside the block, the retry loops this library owns — every Google Drive and
Sheets call, `DriveFacade.copy_file`'s wait for a new file to appear, and
Spotify's per-call retries — check the deadline between attempts. An attempt
is not started once it has passed (`RunDeadlineReached`, a `TimeoutError`,
chained from the previous error), and a backoff that would end at or past it
is skipped, re-raising the error that prompted it. An attempt already in
flight is not interrupted, so leave room for one call's own timeout. Nesting
keeps the earlier deadline; `remaining_seconds()` returns `None` when none is
set, and without one nothing changes.

Cleanup that must happen because the run is out of time — rolling back a
half-finished write — would be refused by the deadline that triggered it.
`without_run_deadline()` clears the deadline for its block and restores it on
exit, so that cleanup gets the ordinary retries:

```python
from mini_app_polis.deadline import without_run_deadline

with without_run_deadline():
    drive.delete_file(half_written_id)
```

Keep such blocks short: they spend the headroom between the deadline and the
hard ceiling. A `run_deadline` nested inside starts afresh and can bound the
cleanup itself.

The deadline is a `contextvars` value: `asyncio.to_thread` and new asyncio
tasks inherit it, a plain `threading.Thread` does not. See the module
docstring.

### Lambda queue consumers

```python
from mini_app_polis.api.contract import TranscriptionRunMessage
from mini_app_polis.lambda_worker import deadline, parse_message, process_records


def lambda_handler(event, context):
    def handle(record):
        message = parse_message(record["body"], TranscriptionRunMessage)
        with deadline(context, request_headroom_seconds=60):
            run(message.payload, run_id=record["messageId"])

    return process_records(event, handle)
```

`deadline` raises `RunOutOfTime` a margin (`margin_seconds`) before the
function's timeout, and sets the run deadline above `request_headroom_seconds`
earlier still. `RunOutOfTime` is a `BaseException`, so a flow's
`except Exception` cannot swallow it. `parse_message` raises
`UnprocessableMessage` for a body that is not a message of the kinds given.
`process_records` never raises, and names each failed record once. The message
kinds are in `mini_app_polis.api.contract` (`QueueMessage`, `JobMessage`).

A run report carries the cog's build when the cog passes its own version:
`run_report(..., version=__version__)` stamps `(version=X.Y.Z)` on the text.
The version comes from the cog's `_version.py`, never from installed package
metadata, which a Lambda deploy strips.

### Logger

```python
from mini_app_polis import logger

logger.info("Starting pipeline")
logger.error("Something went wrong: %s", err)

# Or get the logger instance directly
log = logger.get_logger()
```

Set `LOGGING_LEVEL=INFO` (or DEBUG/WARNING/ERROR) in your environment.

### Timing

```python
from mini_app_polis import timing

with timing.invocation(cog="deejay", mode=mode):
    run()
```

When the block ends, one JSON line goes to stdout: wall and CPU time, the
idle share, and the waiting split by service (`google`, `spotify`,
`anthropic`, `api`, `sleep`, …). CloudWatch Logs Insights reads it as
fields (`timing.idle_pct`, `timing.wait.anthropic.ms`). See the module
docstring for how calls are attributed.

---

## Development

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### First-time setup

Clone the repo and run these commands once in order:
```bash
# 1. Install all dependencies including dev and optional extras
uv sync --all-extras

# 2. Install pre-commit hooks into git
uv run pre-commit install
```

That's it. Pre-commit will now run automatically on every `git commit`.

### Daily workflow
```bash
# Run tests
uv run pytest

# Run tests with coverage detail
uv run pytest --cov=mini_app_polis --cov-report=term-missing

# Lint (auto-fix where possible)
uv run ruff check src/ tests/ --fix

# Format
uv run ruff format src/ tests/

# Type check
uv run mypy src/

# Run all pre-commit hooks manually against all files
uv run pre-commit run --all-files
```

### What pre-commit does

On every `git commit`, the following run automatically:

- **ruff** — lints and auto-fixes what it can
- **ruff-format** — formats code
- **python-check-mock-methods** — catches incorrect mock usage
- **python-use-type-annotations** — flags old-style type comments
- **gitleaks** — blocks commits that contain secrets

If any hook fails, the commit is blocked. Ruff will auto-fix in place — just `git add .` and re-commit.

### Environment variables

Tests need no secrets: `uv run pytest` runs anywhere.

Everything else reads its settings from the process environment, supplied by
[Doppler](https://docs.doppler.com/docs/install-cli). Nothing reads a `.env`
file. Once per machine, then once per clone:

```bash
brew install gnupg dopplerhq/cli/doppler
doppler login
doppler setup        # reads doppler.yaml: mini-app-polis-ecosystem / dev
```

Then run anything that needs secrets under it — the contract suite, say:

```bash
doppler run -- uv run pytest contract --no-cov
```

Local runs use the `dev` config only. `.env.example` lists the names this
library reads; the values live in Doppler. Uncommented names are required,
commented ones optional. Every repo that depends on this library gets the
`check-doppler-keys` console script, which lists any required name that
`dev` is missing (names only, never values):

```bash
uv run check-doppler-keys
```

Every variable the library reads, which module reads it, and whether it is
required is listed in [`.env.example`](.env.example). A caller's own named
API key, `<MACHINE_NAME>_API_KEY`, is derived from its machine name by
`machine_key_env_var()` (`deejay-cog` → `DEEJAY_COG_API_KEY`), so each caller
lists its own.

---

## Releasing

Releases are automated via semantic-release on push to `main`.

| Commit format | Bump | Example result |
|---|---|---|
| `fix: ...` | patch | v1.0.0 -> v1.0.1 |
| `feat: ...` | minor | v1.0.0 -> v1.1.0 |
| `feat!: ...` | major | v1.0.0 -> v2.0.0 |

**Important:** Semantic-release only recognizes [Conventional Commits](https://www.conventionalcommits.org/) format. These will NOT trigger a release:
- `breaking change: ...` - unrecognized type
- `feat: breaking change ...` - the word "breaking" in the message does not count
- Free-form messages with no type prefix

For major bumps, prefer the `BREAKING CHANGE` footer in the commit body as it is more reliably parsed than `feat!`:

```text
feat: your message here

BREAKING CHANGE: description of what changed and why it breaks
```

The footer format (`BREAKING CHANGE:` in the body) is the more battle-tested path across different versions of semantic-release. `feat!` should work per spec but has been known to behave inconsistently depending on plugin versions. Document both and lean on the footer.

---

## Import Namespace

Use `mini_app_polis` as the import namespace across all modules (for example, `from mini_app_polis.api import KaianoApiClient`).
