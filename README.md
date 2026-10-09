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
  "miniapppolis-common-utils>=5.0,<6",
]
```

Then `uv lock`. To move to a newer release, `uv lock --upgrade-package
miniapppolis-common-utils` — no edit to this file.

To use the LLM module, add the `llm` extra:

```toml
dependencies = [
  "miniapppolis-common-utils[llm]>=5.0,<6",
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
| `discord.py` | `from mini_app_polis import discord` | Discord webhook transport: named channels, rate-limit cooldowns, environment labels, never raises |
| `doppler.py` | `from mini_app_polis.doppler import DopplerClient` | Reads and writes secrets in one Doppler config (API or CLI), and the `check-doppler-keys` console script |
| `google/` | `from mini_app_polis.google import GoogleAPI` | Drive + Sheets facade |
| `llm/` | `from mini_app_polis.llm import build_llm, LLMMessage` | OpenAI + Anthropic clients (optional extra) |
| `mp3/` | `from mini_app_polis.mp3 import ...` | AcoustID identification, tagging, renaming |
| `music/` | `from mini_app_polis.music import normalize_for_matching` | Music data normalization utilities |
| `spotify/` | `from mini_app_polis.spotify import SpotifyAPI` | Spotipy wrapper; raises `SpotifyTokenExpired` when Spotify refuses the refresh token (`invalid_grant`, not retried) |
| `timing.py` | `from mini_app_polis import timing` | Per-invocation working vs waiting time, by service, as one CloudWatch line |
| `vdj/` | `from mini_app_polis.vdj.m3u import ParseFacade` | VirtualDJ M3U parsing |

---

## Usage

### KaianoApiClient

```python
from mini_app_polis.api import KaianoApiClient

# Set KAIANO_API_BASE_URL and this machine's own key
# (deejay-cog -> DEEJAY_COG_API_KEY) in the environment.
# The machine name is what selects the key variable, and the key is
# what names this caller to the API. There is no fallback: without it,
# every call returns 401.
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
            project_gid="1218223550488548",
            section_gid="1218337761864701",
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
the hold ends, rather than posting through it. A send waits at most
`MAX_WAIT_SECS` (10 s; `max_wait=` to change it) — a burst larger than that
loses its tail, a Cloudflare 1015 drops what is sent during it — and the
first drop of an episode is reported to Sentry. `webhook_url(..., source=...)` and the `source=` argument on the send
functions read the variables from a mapping instead of the environment.
`post_webhook` is the rate-limit-aware POST underneath, for payloads this
module does not shape (GitHub events forwarded to the webhook's `/github`
suffix). See the module docstring for the rest.

### Activity feed (requires `activity` extra)

Committed data changes to `activity`, faults to `errors`, for a FastAPI
service on SQLAlchemy 2. Needs the `activity` extra and a release that has
these modules: `>=5.0` is not enough, since earlier 5.x releases lack them.
Use the minor release that introduced `mini_app_polis.activity` (and
`mini_app_polis.discord`) as the lower bound:

```toml
dependencies = ["miniapppolis-common-utils[activity]>=<that release>,<6"]
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

Key variables:

| Variable | Used by | Required for |
|---|---|---|
| `KAIANO_API_BASE_URL` | `KaianoApiClient` | Calling internal FastAPI services |
| `<MACHINE_NAME>_API_KEY` | `KaianoApiClient` | This machine's own named key, e.g. `DEEJAY_COG_API_KEY`. Derived from the machine name by `machine_key_env_var()` |
| `KAIANO_API_KEY` | `KaianoApiClient` | Unnamed fallback for a caller that declares no machine name. Authenticates, but its writes are unattributable |
| `ASANA_ACCESS_TOKEN` | `AsanaClient` | Asana personal access token. Read per request, so rotation needs no restart |
| `ASANA_WORKSPACE_ID` | `AsanaClient` | Workspace gid. Required only by `find_or_create_tag()`, since tags are workspace-scoped objects |
| `LOGGING_LEVEL` | `logger` | Log verbosity (`INFO` default). Importing `logger` calls `basicConfig`, so this reaches every library in the process. Loggers known to print credentials (`websockets`, `httpx`, `httpcore`, …) are held at INFO regardless — raising this to DEBUG will not print your secrets |
| `GOOGLE_CREDENTIALS_JSON` | `GoogleAPI` | Google Drive + Sheets access |
| `SPOTIPY_CLIENT_ID` | `SpotifyAPI` | Spotify operations |
| `SPOTIPY_CLIENT_SECRET` | `SpotifyAPI` | Spotify operations |
| `SPOTIPY_REFRESH_TOKEN` | `SpotifyAPI` | Spotify operations |
| `DISCORD_WEBHOOK_URL` | `discord`, `activity` | Fallback webhook for any channel whose own variable is unset |
| `DISCORD_WEBHOOK_URL_DEFAULT` / `_ERRORS` / `_ACTIVITY` / `_RUNS` | `discord`, `activity` | One webhook per channel. Optional; each falls back to `DISCORD_WEBHOOK_URL` |
| `ANTHROPIC_API_KEY` | `llm` extra | Anthropic LLM calls |
| `OPENAI_API_KEY` | `llm` extra | OpenAI LLM calls |
| `DOPPLER_TOKEN` / `DOPPLER_PROJECT` / `DOPPLER_CONFIG` | `DopplerClient.from_env()` | Writing a secret back to Doppler from a service. The token is a service token with write access to that one config; Doppler's syncs inject the other two |

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
