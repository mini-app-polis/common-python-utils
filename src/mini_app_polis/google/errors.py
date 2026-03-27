class GoogleAPIError(RuntimeError):
    """Base error for mini_app_polis.google."""


class NotFoundError(GoogleAPIError):
    """Requested resource was not found."""
