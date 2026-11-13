"""A stand-in for SandboxClient that records calls and never touches the network."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hermes_sandbox.client import ExecResult, Limits, SandboxSession
from hermes_sandbox.errors import SessionExpiredError


def _session(session_id: str, seconds_left: int = 300) -> SandboxSession:
    from datetime import timedelta

    expires = (datetime.now(timezone.utc) + timedelta(seconds=seconds_left)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    return SandboxSession(
        session_id=session_id,
        capability="cap-" + session_id,
        client_public_key=b"\x01" * 32,
        client_private_key=b"\x02" * 32,
        client_nonce="nonce",
        state="ACTIVE",
        tier="standard",
        duration_seconds=300,
        limits=Limits(),
        guest_public_key=b"\x03" * 32,
        lease_expires_at=expires,
    )


class FakeClient:
    """Records every call so tests can assert on lifecycle, not just results."""

    def __init__(
        self,
        *,
        results: list[ExecResult] | None = None,
        lease_seconds: int = 300,
        create_error: Exception | None = None,
        archive: bytes | None = b"workspace-archive",
        fail_on_exec: int | None = None,
    ):
        self.created: list[str] = []
        self.create_kwargs: list[dict[str, Any]] = []
        self.destroyed: list[str] = []
        self.cancelled: list[tuple[str, str]] = []
        self.execs: list[dict[str, Any]] = []
        self.puts: list[tuple[str, str, int]] = []
        self.closed = False
        self._results = list(results or [])
        self._lease_seconds = lease_seconds
        self._create_error = create_error
        self._archive = archive
        self._fail_on_exec = fail_on_exec
        self._counter = 0

    # -- session lifecycle ------------------------------------------------

    def create_session(self, **kwargs: Any) -> SandboxSession:
        if self._create_error is not None:
            raise self._create_error
        self._counter += 1
        session_id = f"sbx_{self._counter:03d}"
        self.created.append(session_id)
        self.create_kwargs.append(dict(kwargs))
        return _session(session_id, self._lease_seconds)

    def wait_for_ready(self, session: SandboxSession, timeout_seconds: float = 60) -> dict[str, Any]:
        return {"state": "ACTIVE"}

    def destroy(self, session: SandboxSession) -> None:
        self.destroyed.append(session.session_id)

    # -- operations -------------------------------------------------------

    def exec(self, session: SandboxSession, command: list[str], **kwargs: Any) -> ExecResult:
        self.execs.append({"session_id": session.session_id, "command": command, **kwargs})
        if self._fail_on_exec is not None and len(self.execs) == self._fail_on_exec:
            raise SessionExpiredError("lease_expired", "the lease ended")
        # The login-shell snapshot is core plumbing, not the command under test;
        # answer it without consuming a scripted result.
        joined = command[-1] if command else ""
        if joined.startswith("umask 077"):
            return ExecResult(ok=True, exit_code=0, stdout="")
        if self._results:
            return self._results.pop(0)
        return ExecResult(ok=True, exit_code=0, stdout="ok\n")

    def cancel(self, session: SandboxSession, request_id: str) -> None:
        self.cancelled.append((session.session_id, request_id))

    # -- files ------------------------------------------------------------

    def get_file(self, session: SandboxSession, path: str, *, archive: bool = False) -> bytes:
        if self._archive is None:
            raise AssertionError("test did not expect a workspace capture")
        return self._archive

    def put_file(self, session: SandboxSession, path: str, data: bytes, **kwargs: Any) -> dict[str, Any]:
        self.puts.append((session.session_id, path, len(data)))
        return {"ok": True}

    def close(self) -> None:
        self.closed = True
