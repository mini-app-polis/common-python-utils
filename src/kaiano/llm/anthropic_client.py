from __future__ import annotations

import os
from typing import Any

from kaiano import logger as logger_mod

from ._json import parse_json, validate_json
from .base import LLMClient, LLMConfig
from .errors import LLMError
from .types import LLMMessage, LLMResult

log = logger_mod.get_logger()


class AnthropicLLM(LLMClient):
    """Anthropic Claude client wrapper.

    Uses the Messages API and enforces JSON output via validation.
    """

    def __init__(self, config: LLMConfig):
        self._cfg = config
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise LLMError(
                f"Missing env var {config.api_key_env} for Anthropic API key"
            )

        try:
            from anthropic import Anthropic  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise LLMError(
                "anthropic SDK not installed. Add dependency 'anthropic' or install kaiano with the llm extra."
            ) from e

        self._client = Anthropic(api_key=api_key)

    def _extract_output_text(self, resp: Any) -> str:
        """Concatenate all text blocks from the Claude response.

        Prefers the SDK's get_final_text() when available; otherwise handles
        content blocks as objects or dicts depending on SDK version.
        """
        try:
            if callable(getattr(resp, "get_final_text", None)):
                text = resp.get_final_text()
                if isinstance(text, str) and text.strip():
                    return text.strip()
        except Exception:
            pass

        # Some SDK/API versions wrap the message in .message
        msg = resp
        if getattr(resp, "message", None) is not None:
            msg = resp.message
        content = getattr(msg, "content", None) or []
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "") or ""
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
            else:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", None)
                    if text is None and hasattr(block, "__getitem__"):
                        try:
                            text = block["text"]
                        except (KeyError, TypeError):
                            text = None
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
        out = "\n".join(parts).strip()
        if out:
            return out

        # Diagnostic: include what we saw so we can fix extraction
        block_types = []
        for b in content[:5]:
            if isinstance(b, dict):
                block_types.append(b.get("type", type(b).__name__))
            else:
                block_types.append(getattr(b, "type", type(b).__name__))
        raise LLMError(
            "Unable to extract text from Anthropic response "
            f"(content has {len(content)} blocks, types={block_types})"
        )

    def generate_json(
        self,
        *,
        messages: list[LLMMessage],
        json_schema: dict[str, Any],
        schema_name: str = "output",
    ) -> LLMResult:
        # Anthropic messages: system prompt is a separate field; messages cannot
        # include role="system". We split them here.
        system_parts: list[str] = []
        non_system_messages: list[LLMMessage] = []

        # Kept for API parity with OpenAI structured output; Anthropic does not
        # expose the schema name to the model.
        _ = schema_name

        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            else:
                non_system_messages.append(m)

        if not non_system_messages:
            raise LLMError("Anthropic messages require at least one non-system message")

        system_prompt = "\n\n".join(system_parts) if system_parts else None

        try:
            resp = self._client.messages.create(
                model=self._cfg.model,
                max_tokens=8192,
                system=system_prompt,
                messages=[
                    {"role": m.role, "content": m.content} for m in non_system_messages
                ],
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Anthropic request failed: {e}") from e

        raw = self._extract_output_text(resp)
        if not raw or not raw.strip():
            raise LLMError(
                "Anthropic returned empty or whitespace-only text; cannot parse JSON. "
                "Check that the model is returning valid content blocks."
            )
        # Strip markdown code fence if present (e.g. ```json ... ```)
        s = raw.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            s = "\n".join(lines)
        data = parse_json(s)
        validate_json(data, json_schema)

        return LLMResult(
            provider="anthropic",
            model=self._cfg.model,
            output_json=data,
            raw_text=raw,
        )
