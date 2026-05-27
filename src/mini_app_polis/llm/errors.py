class LLMError(RuntimeError):
    """Represent a generic runtime failure in the LLM layer."""

    pass


class LLMValidationError(LLMError):
    """Raised when the model output cannot be validated against the requested schema."""


class LLMTruncationError(LLMError):
    """Raised when the model's response was cut off because it hit max_tokens.

    The response body is partial and may not be valid JSON; do not attempt
    to parse it. The caller should increase max_tokens via LLMConfig and
    retry, or accept that the input is too dense to extract in one call.
    """
