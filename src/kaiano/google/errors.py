class GoogleAPIError(RuntimeError):
    """Base error for kaiano.google."""


class NotFoundError(GoogleAPIError):
    """Requested resource was not found."""
