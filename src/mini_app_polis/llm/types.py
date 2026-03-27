from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class LLMMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMResult:
    """Provider-neutral result container."""

    provider: str
    model: str
    output_json: dict[str, Any]
    raw_text: str
