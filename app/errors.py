"""Domain-level errors shared by the service and HTTP layers."""

from __future__ import annotations


class AppError(Exception):
    """An error that can be translated into a stable HTTP error response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
