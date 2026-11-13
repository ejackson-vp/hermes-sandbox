"""Errors, mirroring the SDK's distinct failure modes.

The provider must not flatten these. ``SessionExpiredError`` means "roll over to a
new session"; ``SessionBusyError`` means "retry"; ``OutcomeUnknownError`` means
"the command may have run and we will never know". Treating all three as a
generic failure would silently turn a possible side effect into a clean error.
"""

from __future__ import annotations


class SandboxError(Exception):
    """A sandbox API or guest reported an error."""

    def __init__(self, code: str, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id

    @property
    def transient(self) -> bool:
        """Worth retrying without operator involvement."""
        return self.status in (502, 503, 504) or self.code in ("control_plane_unavailable", "provisioning_unavailable")


class SessionExpiredError(SandboxError):
    """The lease or capability is gone. The session cannot be used again."""


class SessionBusyError(SandboxError):
    """Another operation holds the session's single foreground slot."""


class OutcomeUnknownError(SandboxError):
    """The controller restarted with this request in flight.

    The command may have executed. This is terminal and deliberately louder than
    a not-found: a retry could run the command twice.
    """


class ResultExpiredError(SandboxError):
    """A known result aged out of the relay cache (5 minutes)."""


class TransportError(SandboxError):
    """The API could not be reached, or the connection failed mid-request."""
