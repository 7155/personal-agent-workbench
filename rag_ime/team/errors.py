"""Typed errors shared by the team identity and membership boundary."""

from __future__ import annotations


class TeamError(Exception):
    """A safe, transport-ready error from the team server boundary.

    The message is deliberately caller-safe: identity and authorization
    callers can serialize ``status``, ``code`` and ``message`` without
    exposing a password hash, session token, or database implementation
    detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("TeamError status must be an HTTP status code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("TeamError code must be non-empty")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("TeamError message must be non-empty")
        self.status = status
        self.code = code.strip()
        self.message = message.strip()
        super().__init__(self.message)

    def payload(self) -> dict[str, object]:
        """Return the stable JSON shape used by HTTP adapters."""

        return {
            "code": self.code,
            "message": self.message,
        }

    @property
    def http_status(self) -> int:
        """Preserve the typed status through the existing Control HTTP handlers."""
        return self.status

    def response_payload(self) -> dict[str, object]:
        """Use the same public envelope as the authenticated Team HTTP entry."""

        return {"ok": False, "error": self.message, "errorCode": self.code}

    def __repr__(self) -> str:
        return f"TeamError(status={self.status!r}, code={self.code!r}, message={self.message!r})"


__all__ = ["TeamError"]
