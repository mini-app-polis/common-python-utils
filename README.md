# common-python-utils

Common Python utilities shared across Kaiano's projects.

> **Import namespace:** `kaiano`
> **Replaces:** `kaiano-common-utils` (v1.x — frozen, no new features)

---

## Installation

Pin to a specific release tag in your `pyproject.toml`:

```toml
[tool.uv.sources]
kaiano = { git = "https://github.com/mini-app-polis/common-python-utils", tag = "v1.0.0" }

[project]
dependencies = [
  "kaiano",
]
```

To use the LLM module, add the `llm` extra:

```toml
dependencies = [
  "kaiano[llm]",
]
```

---

## Modules

| Module | Import | Description |
|--------|--------|-------------|
| `api/` | `from kaiano.api import KaianoApiClient` | HTTP client for internal FastAPI services |
| `config.py` | `from kaiano import config` | Env-var driven shared config (Spotify, Google, VDJ) |
| `google/` | `from kaiano.google import GoogleAPI` | Drive + Sheets facade |
| `llm/` | `from kaiano.llm import build_llm, LLMMessage` | OpenAI + Anthropic clients (optional extra) |
| `mp3/` | `from kaiano.mp3 import ...` | AcoustID identification, tagging, renaming |
| `spotify/` | `from kaiano.spotify import SpotifyAPI` | Spotipy wrapper |
| `vdj/` | `from kaiano.vdj.m3u import ParseFacade` | VirtualDJ M3U parsing |

---

## Usage

### KaianoApiClient

```python
from kaiano.api import KaianoApiClient

# Set KAIANO_API_BASE_URL and KAIANO_API_CLERK_TOKEN in environment
# Falls back to KAIANO_API_OWNER_ID if no Clerk token is set
client = KaianoApiClient.from_env()
result = client.post("/sets", {"name": "My Set"})
```

### LLM (requires `llm` extra)

```python
from kaiano.llm import build_llm, LLMMessage

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

### Logger

```python
from kaiano import logger

logger.info("Starting pipeline")
logger.error("Something went wrong: %s", err)

# Or get the logger instance directly
log = logger.get_logger()
```

Set `LOGGING_LEVEL=INFO` (or DEBUG/WARNING/ERROR) in your environment.

---

## Development

```bash
# Install with all extras
uv sync --all-extras

# Run tests
uv run pytest

# Lint + format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Pre-commit hooks
uv run pre-commit install
```

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

## Migration from kaiano-common-utils

This repo replaces `kaiano-common-utils` (v1.x). The import namespace (`kaiano`) is unchanged — no consumer code changes needed, only the install source changes.

`kaiano-common-utils` is frozen at v1.1.0 and will receive security patches only.
