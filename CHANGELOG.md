# Changelog

## [2.0.0] — Initial release

Clean-slate replacement for `kaiano-common-utils` v1.x.

### Changes from v1.x

- Repo renamed to `common-python-utils` (install name)
- Import namespace `kaiano` preserved — no consumer code changes required
- `config.py` removed — hardcoded Drive IDs and domain constants moved to consumer repos
- `logger.py` decoupled from `config.py` — reads `LOGGING_LEVEL` directly from env
- `pydrive` and `pydrive2` dependencies removed (unused)
- `pytz` dependency removed — use stdlib `zoneinfo` (Python 3.11+)
- `.npmrc` removed from repo
- Starting version set to `2.0.0` to signal clean break from v1.x line
- LLM module tests added (`tests/kaiano/llm/`)
- VDJ M3U tests carried forward from v1.x
