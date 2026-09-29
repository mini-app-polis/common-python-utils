"""The wrapper every Kaiano API response comes in.

Success bodies are ``Envelope[T]``; error bodies are ``ErrorEnvelope``.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field


class Meta(BaseModel):
    """Pagination metadata included with every envelope response."""

    count: int = Field(..., ge=0, description="Number of items in this response.")
    total: int = Field(..., ge=0, description="Total number of matching items.")
    version: str = Field(..., description="API version string for this response.")


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """TODO: describe this class."""

    data: T = Field(..., description="Semantic value for data.")
    meta: Meta = Field(..., description="Semantic value for meta.")


class ErrorDetail(BaseModel):
    """Structured error payload used across API error responses."""

    code: str = Field(..., description="Semantic value for code.")
    message: str = Field(..., description="Semantic value for message.")
    details: dict | list | str | None = Field(
        default=None, description="Semantic value for details."
    )


class ErrorEnvelope(BaseModel):
    """Top-level API error envelope."""

    error: ErrorDetail = Field(..., description="Semantic value for error.")
