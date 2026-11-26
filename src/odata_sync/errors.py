"""Exceptions raised by odata_sync."""

from __future__ import annotations


class ODataError(Exception):
    """The server answered with an error that retrying will not fix."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(ODataError):
    """401 or 403: the token is missing, expired or lacks permission."""


class RetryExhausted(ODataError):
    """Every retry failed (429, 5xx or network errors)."""
