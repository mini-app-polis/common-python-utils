from __future__ import annotations

from typing import Any

from .anthropic_client import AnthropicLLM
from .base import LLMClient, LLMConfig
from .errors import LLMError
from .openai_client import OpenAILLM


def build_llm(
    *,
    provider: str,
    model: str,
    timeout_s: float | None = None,
    max_retries: int | None = None,
) -> LLMClient:
    """Factory for provider clients.

    Providers:
    - openai (ChatGPT)
    - anthropic / claude (Claude)

    ``timeout_s`` and ``max_retries`` override :class:`LLMConfig`'s defaults.
    A caller running under a deadline — a Lambda cog, say — sets both, because
    the worst case is ``timeout_s × (1 + max_retries)`` and the defaults are
    sized for a process with no deadline at all.

    Extend by adding new provider clients and mapping here.
    """

    p = provider.lower().strip()
    overrides: dict[str, Any] = {}
    if timeout_s is not None:
        overrides["timeout_s"] = timeout_s
    if max_retries is not None:
        overrides["max_retries"] = max_retries

    if p == "openai":
        return OpenAILLM(
            LLMConfig(
                provider="openai",
                model=model,
                api_key_env="OPENAI_API_KEY",
                **overrides,
            )
        )

    if p in ("anthropic", "claude"):
        return AnthropicLLM(
            LLMConfig(
                provider="anthropic",
                model=model,
                api_key_env="ANTHROPIC_API_KEY",
                **overrides,
            )
        )

    raise LLMError(f"Unknown LLM provider: {provider}")
