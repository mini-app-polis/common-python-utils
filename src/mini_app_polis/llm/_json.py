from __future__ import annotations

import json
from typing import Any

from jsonschema import validate
from jsonschema.exceptions import ValidationError as _SchemaValidationError

from .errors import LLMValidationError

_DECODER = json.JSONDecoder()

#: How much of a response an error quotes, so the failure says what the
#: model actually returned without dumping the whole of it into a log line.
_PREVIEW_CHARS = 200


def parse_json(text: str) -> dict[str, Any]:
    """The first JSON object in a model response.

    The provider is told to return JSON only, but models sometimes write
    one valid object and then a stray line ("Extra data"), or a sentence
    before it. Either used to fail the whole call. Stray text before or
    after the object is now ignored; anything that parsed before parses
    the same, and schema validation is unchanged.

    When there is no object, the error quotes the start of the response.
    """
    # The whole text first, so a response that parsed before is returned
    # exactly as before (a top-level array included); the scan is only the
    # fallback for text json.loads refuses.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    except Exception as e:  # noqa: BLE001 — not text at all (None, a mock)
        raise LLMValidationError(f"Failed to parse JSON: {e}") from e
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    preview = " ".join(text.split())[:_PREVIEW_CHARS]
    raise LLMValidationError(
        f"Failed to parse JSON: no JSON object in model response: {preview!r}"
    )


def validate_json(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate parsed JSON data against the provided JSON schema."""
    try:
        validate(instance=instance, schema=schema)
    except _SchemaValidationError as e:
        raise LLMValidationError(f"JSON schema validation failed: {e.message}") from e
