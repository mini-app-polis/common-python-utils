from __future__ import annotations

import json
from typing import Any

from jsonschema import validate
from jsonschema.exceptions import ValidationError as _SchemaValidationError

from .errors import LLMValidationError


def parse_json(text: str) -> dict[str, Any]:
    """Parse JSON from a model response.

    Assumes the provider was instructed to return JSON only.
    """

    try:
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise LLMValidationError(f"Failed to parse JSON: {e}") from e


def validate_json(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        validate(instance=instance, schema=schema)
    except _SchemaValidationError as e:
        raise LLMValidationError(f"JSON schema validation failed: {e.message}") from e
