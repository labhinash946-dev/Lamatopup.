class ApiError(Exception):
    """Raised anywhere; rendered as a uniform JSON error."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
