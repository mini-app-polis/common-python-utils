from __future__ import annotations

from .anthropic_client import AnthropicLLM
from .base import LLMClient, LLMConfig
from .errors import LLMError
from .openai_client import OpenAILLM


def build_llm(*, provider: str, model: str) -> LLMClient:
    """Factory for provider clients.

    Providers:
    - openai (ChatGPT)
    - anthropic / claude (Claude)

    Extend by adding new provider clients and mapping here.
    """

    p = provider.lower().strip()

    if p == "openai":
        return OpenAILLM(
            LLMConfig(provider="openai", model=model, api_key_env="OPENAI_API_KEY")
        )

    if p in ("anthropic", "claude"):
        return AnthropicLLM(
            LLMConfig(
                provider="anthropic", model=model, api_key_env="ANTHROPIC_API_KEY"
            )
        )

    raise LLMError(f"Unknown LLM provider: {provider}")
