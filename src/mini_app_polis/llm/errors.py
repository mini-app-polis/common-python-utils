class LLMError(RuntimeError):
    """Represent a generic runtime failure in the LLM layer."""

    pass


class LLMValidationError(LLMError):
    """Raised when the model output cannot be validated against the requested schema."""
