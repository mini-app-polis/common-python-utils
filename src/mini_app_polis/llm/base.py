from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .types import LLMMessage, LLMResult


@dataclass(frozen=True)
class LLMConfig:
    """Define provider and runtime settings for an LLM client."""

    provider: str
    model: str
    api_key_env: str
    timeout_s: float = 60.0
    # Per-request output token cap for both Anthropic and OpenAI clients.
    # Anthropic Sonnet 4.6 supports up to 64K; OpenAI limits vary by model.
    # 16384 is a safe default that fits both without being wasteful.
    max_tokens: int = 16384


class LLMClient(Protocol):
    """Small interface for "transcript -> structured notes" style tasks."""

    def generate_json(
        self,
        *,
        messages: list[LLMMessage],
        json_schema: dict[str, Any],
        schema_name: str = "output",
    ) -> LLMResult:
        """Generate structured JSON output validated against a schema."""
        raise NotImplementedError
