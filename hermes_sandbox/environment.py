"""The execution environment Hermes commands actually run in.

Subclasses ``BaseEnvironment`` and implements only ``_run_bash`` and ``cleanup``,
which is the supported seam for SDK-style backends (the same one the built-in
Modal/Daytona/Vercel backends use). Everything else comes from core:

* timeout enforcement, interrupt handling and output draining (``_wait_for_process``)
* stdin transport, via ``_stdin_mode = "payload"``
* cwd tracking and the login-shell snapshot (``init_session``)
* background-process bookkeeping

The one rule that is not negotiable: there is no local fallback. If the sandbox
API is unreachable, the command fails. A provider that quietly ran on the host
when the API was down would turn "isolated execution" into a claim rather than a
property.
"""

from __future__ import annotations

import logging
import shlex
import threading
import uuid
from typing import Any

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle

from .client import ExecResult, SandboxClient, SandboxSession
from .config import SandboxConfig
from .errors import SessionExpiredError, SandboxError, TransportError

logger = logging.getLogger(__name__)

#: Exit codes chosen to match shell conventions rather than inventing new ones.
EXIT_TIMEOUT = 124  # as GNU timeout
EXIT_CANCELLED = 130  # SIGINT
EXIT_CANNOT_EXECUTE = 126  # found but not executable / spawn failure
EXIT_PROVISION_FAILED = 125


class SandboxEnvironment(BaseEnvironment):
    """Commands run in a leased Kata microVM through the x402 sandbox API."""

    #: stdin travels in the sealed envelope: the guest never gets a pipe we
    #: cannot seal, and the payload cannot leak into a shell argv.
    _stdin_mode = "payload"

    #: File tools must go through execute(), i.e. into the sandbox. Never set this.
    uses_host_filesystem = False

    #: cleanup() destroys the sandbox, so its processes are gone.
    cleanup_stops_processes = True

    #: No sudo probe: there is no passwordless sudo to discover in the guest, and
    #: a probe would spend a round trip on every session.
    _passwordless_sudo_probe_supported = False

    def __init__(
        self,
        *,
        config: SandboxConfig,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: str | None = None,
        client: SandboxClient | None = None,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.config = config
        self.task_id = task_id
        self.requested_image = image or config.image
        # Injectable so tests can drive the lifecycle without a cluster.
        self._client = client or SandboxClient(
            config.api_url,
            timeout_seconds=config.request_timeout_seconds,
            verify_tls=config.verify_tls,
        )
        self._lock = threading.RLock()
        self._session: SandboxSession | None = None
        self._generation = 0
        self._closed = False
        self._bootstrapped = False
        #: Sessions this object created and still owns, so cleanup only ever
        #: destroys its own leases.
        self._owned: list[str] = []

    # ------------------------------------------------------------ diagnostics

    @property
    def session_id(self) -> str | None:
        return self._session.session_id if self._session else None

    @property
    def generation(self) -> int:
        """How many sandboxes this environment has consumed.

        Increments on every rollover. Hermes surfaces it through the doctor
        checks and logs, so "did my task move to a new VM" is answerable.
        """
        return self._generation

    def describe(self) -> dict[str, Any]:
        session = self._session
        return {
            "backend": "sandbox",
            "task_id": self.task_id,
            "session_id": session.session_id if session else None,
            "state": session.state if session else None,
            "generation": self._generation,
            "remaining_seconds": session.remaining_seconds() if session else None,
            "image": self.requested_image,
            "tier": self.config.tier,
            "api_url": self.config.api_url,
            "cwd": self.cwd,
        }

    # -------------------------------------------------------------- lifecycle

    def _ensure_session(self) -> SandboxSession:
        """The current usable session, creating or rolling over as needed."""
        with self._lock:
            if self._closed:
                raise SandboxError("environment_closed", "this sandbox environment has been cleaned up")
            if self._session is not None and self._is_usable(self._session):
                return self._session
            return self._rollover()

    def _is_usable(self, session: SandboxSession) -> bool:
        if session.state in ("EXPIRED", "DESTROYED", "TERMINATED", "FAILED"):
            return False
        remaining = session.remaining_seconds()
        if remaining is None:
            return True
        return remaining > self.config.min_remaining_seconds

    def _rollover(self) -> SandboxSession:
        """Replace the current session, carrying the workspace where possible.

        Called with ``self._lock`` held. This is a *task* continuation, not a
        process resume: the new guest has a new kernel, a new keypair and no
        running processes. Anything the previous session left running is gone.
        """
        previous = self._session
        carried: bytes | None = None
        carry_error: str | None = None
        if previous is not None and self.config.carry_workspace:
            carried, carry_error = self._capture_workspace(previous)

        if previous is not None:
            self._generation += 1
            logger.info(
                "sandbox rollover: task=%s generation=%s old=%s reason=%s carried=%s",
                self.task_id,
                self._generation,
                previous.session_id,
                "lease or state no longer usable",
                len(carried) if carried else 0,
            )
            self._destroy_quietly(previous)

        session = self._client.create_session(**self.config.session_kwargs())
        self._owned.append(session.session_id)
        self._client.wait_for_ready(session, timeout_seconds=self.config.create_timeout_seconds)
        self._session = session
        self._bootstrapped = False

        if carried:
            try:
                self._client.put_file(session, "/workspace", carried, archive=True)
            except SandboxError as exc:
                # A partial carry is worse than a loud one: the agent must know
                # its old files may be missing rather than assume they are there.
                logger.warning("sandbox rollover: workspace carry failed: %s", exc)
        elif carry_error:
            logger.warning("sandbox rollover: workspace not carried: %s", carry_error)
        return session

    def _capture_workspace(self, session: SandboxSession) -> tuple[bytes | None, str | None]:
        """Archive the workspace so a replacement session can start with it."""
        try:
            archive = self._client.get_file(session, "/workspace", archive=True)
        except SessionExpiredError:
            return None, "the previous lease had already expired, so its workspace was unreachable"
        except SandboxError as exc:
            if exc.code in ("file_too_large", "quota_exceeded"):
                return None, f"workspace exceeded the transfer limit ({exc.code})"
            return None, f"{type(exc).__name__}: {exc}"
        limit = session.limits.max_file_bytes
        if len(archive) > limit:
            return None, f"workspace archive is {len(archive)} bytes, over the {limit} byte limit"
        return archive, None

    def _bootstrap(self) -> None:
        """Run the one-time login-shell snapshot inside the sandbox."""
        if self._bootstrapped:
            return
        self._bootstrapped = True
        try:
            self.init_session()
        except Exception as exc:  # noqa: BLE001 - init_session already degrades gracefully
            logger.warning("sandbox: session snapshot failed, falling back to login shell: %s", exc)

    # ---------------------------------------------------------------- execute

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> Any:
        argv = ["bash", "-l", "-c", cmd_string] if login else ["bash", "-c", cmd_string]
        effective_timeout = min(int(timeout or self.timeout), self.config.max_command_seconds)
        state: dict[str, Any] = {"cancelled": False, "request_id": None, "session_id": None}

        def cancel() -> None:
            # No lock: reading the references is atomic, and blocking here would
            # leave a cancel waiting behind a create or a workspace capture.
            state["cancelled"] = True
            request_id, session = state["request_id"], self._session
            if not request_id or session is None:
                return
            try:
                self._client.cancel(session, request_id)
            except SandboxError as exc:
                # Best effort: the base is already killing this command.
                logger.info("sandbox: cancel of %s failed: %s", request_id, exc)

        def exec_fn() -> tuple[str, int]:
            if state["cancelled"]:
                return ("[sandbox] command cancelled before it started", EXIT_CANCELLED)
            try:
                session = self._ensure_session()
            except Exception as exc:  # noqa: BLE001 - any provisioning failure must be reported, not raised
                # Broad on purpose: `_ThreadedProcessHandle` turns an escaping
                # exception into a bare returncode=1 with no output, which would
                # read like a command failure instead of "no sandbox".
                return (_provision_failure(exc), EXIT_PROVISION_FAILED)
            self._bootstrap()

            request_id = uuid.uuid4().hex
            state["request_id"], state["session_id"] = request_id, session.session_id
            try:
                result = self._client.exec(
                    session,
                    argv,
                    cwd=self.cwd,
                    env=self._guest_env(),
                    stdin=stdin_data,
                    timeout_ms=effective_timeout * 1000,
                    request_id=request_id,
                )
            except SessionExpiredError as exc:
                # The lease died mid-command. Retry exactly once in a fresh
                # session; a loop here would spin against an expiring fleet.
                logger.warning("sandbox: lease expired during command, retrying in a new session: %s", exc)
                with self._lock:
                    # Keep the expired handle so the rollover records the
                    # generation and attempts a workspace carry.
                    session = self._rollover()
                state["session_id"] = session.session_id
                request_id = uuid.uuid4().hex
                state["request_id"] = request_id
                try:
                    result = self._client.exec(
                        session,
                        argv,
                        cwd=self.cwd,
                        env=self._guest_env(),
                        stdin=stdin_data,
                        timeout_ms=effective_timeout * 1000,
                        request_id=request_id,
                    )
                except (SandboxError, TransportError) as retry_exc:
                    return (_exec_failure(retry_exc), _failure_code(retry_exc))
                except Exception as retry_exc:  # noqa: BLE001
                    return (_exec_failure(retry_exc), EXIT_CANNOT_EXECUTE)
            except TransportError as exc:
                return (_exec_failure(exc), EXIT_CANNOT_EXECUTE)
            except SandboxError as exc:
                return (_exec_failure(exc), _failure_code(exc))
            except Exception as exc:  # noqa: BLE001 - never let a client bug read as a command result
                return (_exec_failure(exc), EXIT_CANNOT_EXECUTE)

            if state["cancelled"] and not result.cancelled:
                # The base killed us; the guest may have finished first. Report
                # the cancellation rather than a success the caller discarded.
                return (_format(result, note="cancelled by Hermes"), EXIT_CANCELLED)
            return (_format(result), _exit_code(result))

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def _guest_env(self) -> dict[str, str] | None:
        """The only environment the guest ever sees.

        Default is nothing: the guest image provides PATH and friends. An explicit
        allowlist exists for skills that genuinely need a value, and it is a
        separate opt-in from the host environment, which is never forwarded.
        """
        if not self.config.env_allowlist:
            return None
        import os

        selected = {
            name: os.environ[name] for name in self.config.env_allowlist if name in os.environ
        }
        return selected or None

    # ---------------------------------------------------------------- cleanup

    def cleanup(self) -> None:
        """Destroy every session this environment still owns.

        Raises only while a sandbox may still be live, which is the documented
        contract: Hermes settles background pollers as lost once cleanup has
        returned without raising.
        """
        # Hermes v0.23.1's BaseEnvironment has no long-lived process mixin, so
        # this must not assume one. Newer builds gain the mixin and the call
        # starts happening on its own.
        stop_all = getattr(self, "stop_all_processes", None)
        if callable(stop_all):
            stop_all()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            session = self._session
            self._session = None
        if session is not None:
            self._destroy_or_raise(session)
        # Anything left from a rollover that we failed to destroy earlier.
        leftovers = [sid for sid in self._owned if sid != (session.session_id if session else None)]
        for session_id in leftovers:
            logger.info("sandbox: leftover session %s (not destroyed earlier)", session_id)

    def _destroy_or_raise(self, session: SandboxSession) -> None:
        try:
            self._client.destroy(session)
        except SessionExpiredError:
            return
        except TransportError as exc:
            logger.error("sandbox: could not confirm destroy of %s: %s", session.session_id, exc)
            raise RuntimeError(
                f"sandbox {session.session_id} may still be running: {exc}"
            ) from exc
        except SandboxError as exc:
            logger.error("sandbox: destroy of %s failed: %s", session.session_id, exc)
            raise RuntimeError(f"sandbox {session.session_id} may still be running: {exc}") from exc
        finally:
            self._owned = [sid for sid in self._owned if sid != session.session_id]

    def _destroy_quietly(self, session: SandboxSession) -> None:
        try:
            self._client.destroy(session)
        except SandboxError as exc:
            logger.info("sandbox: rollover destroy of %s failed: %s", session.session_id, exc)
        finally:
            self._owned = [sid for sid in self._owned if sid != session.session_id]

    def close(self) -> None:
        """Release the HTTP client. Separate from cleanup() so tests can reuse it."""
        self._client.close()


def _exit_code(result: ExecResult) -> int:
    if result.timed_out:
        return EXIT_TIMEOUT
    if result.cancelled:
        return EXIT_CANCELLED
    if not result.ok:
        return EXIT_CANNOT_EXECUTE
    if result.exit_code is None:
        # A guest that reports ok with no code is a protocol violation; do not
        # invent success.
        return EXIT_CANNOT_EXECUTE
    return int(result.exit_code)


def _format(result: ExecResult, note: str | None = None) -> str:
    """Render a guest result as tool output.

    Failures keep their message in the output rather than only in the exit code,
    because ``_ThreadedProcessHandle`` drops an exception into ``returncode=1``
    with no text, and a bare exit code is not a useful tool response.
    """
    parts = [result.output]
    if result.timed_out:
        limit = result.duration_ms or 0
        parts.append(f"\n[sandbox] command timed out after {limit} ms and was stopped")
    if result.cancelled:
        parts.append("\n[sandbox] command was cancelled")
    if not result.ok and result.error_code:
        parts.append(f"\n[sandbox] {result.error_code}: {result.error_message or 'no detail'}")
    if result.truncated_stdout or result.truncated_stderr:
        parts.append("\n[sandbox] output was truncated at the guest's maxOutputBytes limit")
    if note:
        parts.append(f"\n[sandbox] {note}")
    return "".join(parts)


def _provision_failure(exc: Exception) -> str:
    return (
        "[sandbox] could not provision a sandbox, so the command did not run. "
        f"This is a failure, not a fallback: nothing was executed on the host.\n{type(exc).__name__}: {exc}"
    )


def _exec_failure(exc: Exception) -> str:
    if isinstance(exc, TransportError):
        return (
            "[sandbox] the sandbox API became unreachable during the command; "
            f"its result is unknown and nothing was executed on the host.\n{exc}"
        )
    return f"[sandbox] command failed before completion.\n{type(exc).__name__}: {exc}"


def _failure_code(exc: Exception) -> int:
    if isinstance(exc, SandboxError) and exc.code == "path_outside_workspace":
        return EXIT_CANNOT_EXECUTE
    return EXIT_CANNOT_EXECUTE


def _quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(word) for word in argv)
