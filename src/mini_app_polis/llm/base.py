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
    #: Per-request timeout, in seconds, for both providers. The OpenAI
    #: client passes it on each call; the Anthropic client gives it to the
    #: SDK client, which applies it per request — it used to be declared
    #: here and ignored, leaving Anthropic on the SDK's 600-second default.
    #: A caller whose generation legitimately runs long sets it rather than
    #: inheriting this.
    timeout_s: float = 60.0
    #: Attempts after the first, inside the SDK. ``None`` leaves each SDK's
    #: own default (two). It matters to a caller with a deadline: a timeout
    #: is a per-request cap, so the worst case is ``timeout_s × (1 +
    #: max_retries)``.
    max_retries: int | None = None
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
