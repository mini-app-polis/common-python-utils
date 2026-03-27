from __future__ import annotations

import copy
import os
from typing import Any

from kaiano import logger as logger_mod

from ._json import parse_json, validate_json
from .base import LLMClient, LLMConfig
from .errors import LLMError
from .types import LLMMessage, LLMResult

log = logger_mod.get_logger()


def _schema_strict_for_api(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of the schema shaped for OpenAI Responses API.

    The API requires:
    - Every object has additionalProperties: false.
    - Every object with properties has required: [all property keys].
    - oneOf is not permitted; we replace it with the first subschema.
    We use this only for the API request; validation uses the original schema.
    """
    out = copy.deepcopy(schema)
    out["additionalProperties"] = False

    def fix(o: Any) -> None:
        if isinstance(o, dict):
            if "oneOf" in o and isinstance(o["oneOf"], list) and o["oneOf"]:
                first = copy.deepcopy(o["oneOf"][0])
                o.clear()
                o.update(first)
                fix(o)
                return
            if o.get("type") == "object" and "properties" in o:
                o["additionalProperties"] = False
                o["required"] = list(o["properties"].keys())
            for v in o.values():
                fix(v)
        elif isinstance(o, list):
            for x in o:
                fix(x)

    fix(out)
    return out


class OpenAILLM(LLMClient):
    """OpenAI client wrapper.

    Supports two modes:
    - Structured Outputs (preferred) via Responses API json_schema
    - Fallback to JSON-only text output (still validated)
    """

    def __init__(self, config: LLMConfig):
        self._cfg = config
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise LLMError(f"Missing env var {config.api_key_env} for OpenAI API key")

        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise LLMError(
                "openai SDK not installed. Add dependency 'openai' or install kaiano with the llm extra."
            ) from e

        self._client = OpenAI(api_key=api_key)

    def _extract_output_text(self, resp: Any) -> str:
        # Newer SDKs expose output_text
        text = getattr(resp, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        # Fallback: try common shapes
        try:
            # responses: resp.output is a list of items with .content[]
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) in ("output_text", "text"):
                        t = getattr(c, "text", None)
                        if isinstance(t, str) and t.strip():
                            return t.strip()
        except Exception:
            pass

        raise LLMError("Unable to extract text from OpenAI response")

    def generate_json(
        self,
        *,
        messages: list[LLMMessage],
        json_schema: dict[str, Any],
        schema_name: str = "output",
    ) -> LLMResult:
        # Prefer Responses API Structured Outputs (use strict schema so API accepts it)
        try:
            strict_schema = _schema_strict_for_api(json_schema)
            resp = self._client.responses.create(
                model=self._cfg.model,
                input=[{"role": m.role, "content": m.content} for m in messages],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": strict_schema,
                        "strict": True,
                    }
                },
                timeout=self._cfg.timeout_s,
            )
            raw = self._extract_output_text(resp)
            data = parse_json(raw)
            validate_json(data, json_schema)
            return LLMResult(
                provider="openai", model=self._cfg.model, output_json=data, raw_text=raw
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "OpenAI structured output failed; falling back to JSON-only. err=%s", e
            )

        # Fallback: JSON-only response
        resp2 = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[{"role": m.role, "content": m.content} for m in messages]
            + [
                {
                    "role": "system",
                    "content": "Return ONLY valid JSON matching the requested schema. No markdown, no prose.",
                }
            ],
            temperature=0.2,
            timeout=self._cfg.timeout_s,
        )
        raw2 = (resp2.choices[0].message.content or "").strip()
        data2 = parse_json(raw2)
        validate_json(data2, json_schema)
        return LLMResult(
            provider="openai", model=self._cfg.model, output_json=data2, raw_text=raw2
        )
