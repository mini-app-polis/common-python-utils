class KaianoApiError(Exception):
    def __init__(self, status_code: int, message: str, path: str):
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"KaianoApiError {status_code} on {path}: {message}")
