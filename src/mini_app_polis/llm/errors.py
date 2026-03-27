class LLMError(RuntimeError):
    pass


class LLMValidationError(LLMError):
    """Raised when the model output cannot be validated against the requested schema."""
