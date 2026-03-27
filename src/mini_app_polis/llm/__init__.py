"""LLM provider abstractions (OpenAI / Anthropic / Gemini, etc.).

Design goals:
- Keep provider-specific SDKs isolated.
- Provide a small, stable interface for "generate structured JSON" use cases.
- Validate output against a JSON Schema for deterministic downstream rendering.
"""

from .factory import build_llm
from .types import LLMMessage, LLMResult

__all__ = ["LLMMessage", "LLMResult", "build_llm"]
