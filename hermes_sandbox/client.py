"""HTTP client for the x402 sandbox API.

Written against the endpoints in ``docs/api.md`` and the behaviour of
``sdk/src/runtime.ts``, not against a guess: the envelope shape, the
``payloadHash`` binding and the retry rules are all reproduced from there.

The client is deliberately synchronous. Hermes drives every backend from a worker
thread through ``_ThreadedProcessHandle``, so an async client would add a loop to
own without buying anything.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .canonical import canonical_json
from .crypto import (
    b64decode,
    b64encode,
    generate_keypair,
    generate_nonce,
    open_sealed,
    seal_to,
    sha256_hex,
)
from .errors import (
    OutcomeUnknownError,
    ResultExpiredError,
    SandboxError,
    SessionBusyError,
    SessionExpiredError,
    TransportError,
)
from .payment import MockPayer, payment_signature_header

#: One active foreground operation per sandbox, so a busy answer is normal.
BUSY_RETRY_SECONDS = 0.25
DEFAULT_WAIT_MS = 30_000


@dataclass
class Limits:
    cpu_millis: int = 1000
    memory_mib: int = 1024
    disk_mib: int = 1024
    max_output_bytes: int = 262_144
    max_file_bytes: int = 8 * 1024 * 1024
    max_command_seconds: int = 300
    max_processes: int = 256

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> Limits:
        raw = raw or {}
        return cls(
            cpu_millis=raw.get("cpuMillis", 1000),
            memory_mib=raw.get("memoryMiB", 1024),
            disk_mib=raw.get("diskMiB", 1024),
            max_output_bytes=raw.get("maxOutputBytes", 262_144),
            max_file_bytes=raw.get("maxFileBytes", 8 * 1024 * 1024),
            max_command_seconds=raw.get("maxCommandSeconds", 300),
            max_processes=raw.get("maxProcesses", 256),
        )


@dataclass
class SandboxSession:
    """A handle to one leased sandbox.

    ``guest_public_key`` is the key every command is sealed to. It is replaced on
    resume, because a resumed session boots a fresh guest with a new keypair.
    """

    session_id: str
    capability: str
    client_public_key: bytes
    client_private_key: bytes
    client_nonce: str
    state: str
    tier: str = "standard"
    duration_seconds: int = 300
    limits: Limits = field(default_factory=Limits)
    guest_public_key: bytes | None = None
    lease_expires_at: str | None = None
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def remaining_seconds(self, now: float | None = None) -> int | None:
        """Seconds left on the lease, or ``None`` when the server gave no expiry."""
        if not self.lease_expires_at:
            return None
        target = _parse_iso8601(self.lease_expires_at)
        if target is None:
            return None
        return int(target - (now if now is not None else time.time()))


@dataclass
class ExecResult:
    """A guest command result, including the parts that are not success."""

    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int | None = None
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    error_code: str | None = None
    error_message: str | None = None
    request_id: str | None = None

    @property
    def output(self) -> str:
        """Combined stream, stdout then stderr.

        The guest captures the two separately, so interleaving order is lost.
        Hermes' local backend merges stderr into stdout (``stderr=STDOUT``), so
        merging here keeps the tool contract identical at the cost of ordering.
        """
        if not self.stderr:
            return self.stdout
        if not self.stdout:
            return self.stderr
        return self.stdout + self.stderr


def _parse_iso8601(value: str) -> float | None:
    from datetime import datetime, timezone

    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


class SandboxClient:
    """Talks to one sandbox API. Holds no session state of its own."""

    def __init__(
        self,
        api_url: str,
        *,
        timeout_seconds: float = 60.0,
        verify_tls: bool = True,
        user_agent: str = "hermes-sandbox/0.1.0",
    ):
        self.api_url = api_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.api_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            verify=verify_tls,
            headers={"user-agent": user_agent, "accept": "application/json"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TransportError("transport_error", f"{method} {path} failed: {exc}") from exc

    def _error_from(self, response: httpx.Response, fallback: str) -> SandboxError:
        code, message = fallback, f"HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw_code = body.get("error")
            if isinstance(raw_code, str):
                code = raw_code
            raw_message = body.get("message") or body.get("detail")
            if isinstance(raw_message, str):
                message = raw_message
        if response.status_code == 410 or code in ("lease_expired", "capability_expired"):
            return SessionExpiredError(code, message, response.status_code)
        return SandboxError(code, message, response.status_code)

    # --------------------------------------------------------------- images

    def list_images(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/images")
        if response.status_code != 200:
            raise self._error_from(response, "images_failed")
        body = response.json()
        images = body.get("images") if isinstance(body, dict) else None
        return list(images or [])

    # -------------------------------------------------------------- sessions

    def create_session(
        self,
        *,
        tier: str = "standard",
        duration_seconds: int | None = None,
        cpu_millis: int | None = None,
        memory_mib: int | None = None,
        disk_mib: int | None = None,
        max_lifetime_seconds: int | None = None,
        idle_policy: dict[str, Any] | None = None,
        payer: MockPayer | None = None,
        queued_retries: int = 8,
    ) -> SandboxSession:
        """Create a paid session.

        Mock mode still performs the full x402 handshake: 402, sign, retry. The
        price is bound to the requested spec, so the same spec must be sent both
        times or the server answers ``409 spec_mismatch``.
        """
        public_key, private_key = generate_keypair()
        nonce = generate_nonce()
        client_public_key = b64encode(public_key)
        client_nonce = b64encode(nonce)

        body: dict[str, Any] = {
            "tier": tier,
            "clientPublicKey": client_public_key,
            "clientNonce": client_nonce,
        }
        if tier == "custom":
            body["durationSeconds"] = duration_seconds
            if cpu_millis is not None:
                body["cpuMillis"] = cpu_millis
            if memory_mib is not None:
                body["memoryMiB"] = memory_mib
            if disk_mib is not None:
                body["diskMiB"] = disk_mib
        elif duration_seconds is not None:
            body["durationSeconds"] = duration_seconds
        if max_lifetime_seconds is not None:
            body["maxLifetimeSeconds"] = max_lifetime_seconds
        if idle_policy is not None:
            body["idlePolicy"] = idle_policy

        payer = payer or MockPayer()
        payload_body = body
        payment_headers: dict[str, str] = {}
        challenged = False
        attempt = 0

        while True:
            response = self._request("POST", "/v1/sessions", json=payload_body, headers=payment_headers)
            if response.status_code in (200, 201):
                return self._session_from(response.json(), public_key, private_key, client_nonce)
            if response.status_code == 402:
                # One challenge per create. A second 402 after a signature means
                # the price or the payTo changed underneath us, which is a spec
                # mismatch rather than something to loop on.
                if challenged:
                    raise self._error_from(response, "payment_rejected")
                challenged = True
                requirements, challenge_nonce = self._challenge_from(response)
                payment = payer.build_payment_payload(requirements)
                signature = payer.sign_commitment(
                    payment_payload=payment,
                    client_public_key=client_public_key,
                    client_nonce=client_nonce,
                    challenge_nonce=challenge_nonce,
                )
                payload_body = {**body, "challengeNonce": challenge_nonce, "clientSignature": signature}
                payment_headers = {"PAYMENT-SIGNATURE": payment_signature_header(payment)}
                continue
            if response.status_code == 429 and attempt < queued_retries:
                attempt += 1
                time.sleep(_retry_after(response, attempt))
                continue
            raise self._error_from(response, "create_failed")

    def _challenge_from(self, response: httpx.Response) -> tuple[dict[str, Any], str]:
        """Decode the 402 challenge into ``(requirements, challengeNonce)``."""
        header = response.headers.get("payment-required") or response.headers.get("PAYMENT-REQUIRED")
        if not header:
            raise SandboxError("challenge_missing", "402 without a PAYMENT-REQUIRED header")
        try:
            decoded = json.loads(b64decode(header))
        except ValueError as exc:
            raise SandboxError("challenge_malformed", f"PAYMENT-REQUIRED did not decode: {exc}") from exc
        accepts = decoded.get("accepts") or []
        if not accepts:
            raise SandboxError("challenge_malformed", "the challenge advertised no payment options")
        requirements = accepts[0]
        extra = requirements.get("extra") or {}
        challenge_nonce = extra.get("challengeNonce")
        if not isinstance(challenge_nonce, str):
            raise SandboxError(
                "challenge_malformed",
                "the server did not issue a challengeNonce; a v2 commitment cannot be signed",
            )
        return requirements, challenge_nonce

    def _session_from(
        self,
        body: dict[str, Any],
        public_key: bytes,
        private_key: bytes,
        client_nonce: str,
    ) -> SandboxSession:
        view = body.get("session") or {}
        guest_public_key = view.get("guestPublicKey") or body.get("guestPublicKey")
        return SandboxSession(
            session_id=body["sessionId"],
            capability=body["capability"],
            client_public_key=public_key,
            client_private_key=private_key,
            client_nonce=client_nonce,
            state=body.get("state") or view.get("state") or "PAID",
            tier=body.get("tier") or view.get("tier") or "standard",
            duration_seconds=int(body.get("durationSeconds") or view.get("durationSeconds") or 0),
            limits=Limits.from_json(body.get("limits") or view.get("limits")),
            guest_public_key=b64decode(guest_public_key) if guest_public_key else None,
            lease_expires_at=view.get("leaseExpiresAt"),
            created_at=view.get("createdAt"),
            raw=body,
        )

    def _auth(self, session: SandboxSession) -> dict[str, str]:
        return {"X-Sandbox-Capability": session.capability}

    def status(self, session: SandboxSession) -> dict[str, Any]:
        response = self._request("GET", f"/v1/sessions/{session.session_id}", headers=self._auth(session))
        if response.status_code != 200:
            raise self._error_from(response, "status_failed")
        view = response.json()
        session.state = view.get("state", session.state)
        session.lease_expires_at = view.get("leaseExpiresAt") or session.lease_expires_at
        if view.get("limits"):
            session.limits = Limits.from_json(view["limits"])
        guest_public_key = view.get("guestPublicKey")
        if guest_public_key:
            session.guest_public_key = b64decode(guest_public_key)
        session.raw = {**session.raw, **view}
        return view

    #: States in which a session can accept operations. The API reports `READY`
    #: once the guest is up (the docs example shows `ACTIVE`, which is what an
    #: in-flight session reports), so both are accepted.
    READY_STATES = ("READY", "ACTIVE")

    def wait_for_ready(self, session: SandboxSession, timeout_seconds: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = 0.05
        while time.monotonic() < deadline:
            view = self.status(session)
            state = view.get("state")
            if state in self.READY_STATES and session.guest_public_key:
                return view
            if state in ("FAILED", "EXPIRED", "DESTROYED", "TERMINATED"):
                raise SessionExpiredError(
                    "session_not_ready",
                    f"session reached {state} before becoming ready",
                )
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        raise SandboxError("ready_timeout", f"session not ready within {timeout_seconds:.0f}s")

    def destroy(self, session: SandboxSession) -> None:
        response = self._request("DELETE", f"/v1/sessions/{session.session_id}", headers=self._auth(session))
        # Idempotent by contract: already gone is success.
        if response.status_code in (200, 202, 204, 404, 410):
            return
        raise self._error_from(response, "destroy_failed")

    def suspend(self, session: SandboxSession) -> dict[str, Any]:
        response = self._request("POST", f"/v1/sessions/{session.session_id}/suspend", headers=self._auth(session))
        if response.status_code not in (200, 202):
            raise self._error_from(response, "suspend_failed")
        return response.json()

    def resume(self, session: SandboxSession, timeout_seconds: float = 120.0) -> dict[str, Any]:
        response = self._request("POST", f"/v1/sessions/{session.session_id}/resume", headers=self._auth(session))
        if response.status_code not in (200, 202):
            raise self._error_from(response, "resume_failed")
        # A resumed guest boots fresh, with a new keypair: never reuse the old one.
        session.guest_public_key = None
        return self.wait_for_ready(session, timeout_seconds)

    # ------------------------------------------------------------ operations

    def _seal(self, session: SandboxSession, operation: dict[str, Any]) -> tuple[str, str]:
        if not session.guest_public_key:
            raise SandboxError("no_guest_key", "session has no guest public key yet; wait for ready")
        payload_hash = sha256_hex(canonical_json(operation))
        envelope = b64encode(seal_to(session.guest_public_key, canonical_json(operation).encode("utf-8")))
        return envelope, payload_hash

    def _open(self, session: SandboxSession, envelope: str) -> dict[str, Any]:
        raw = open_sealed(b64decode(envelope), session.client_public_key, session.client_private_key)
        return json.loads(raw)

    def _operation(
        self,
        session: SandboxSession,
        operation: dict[str, Any],
        *,
        timeout_ms: int,
        queue_ms: int,
    ) -> dict[str, Any]:
        if not session.guest_public_key:
            self.wait_for_ready(session)
        request_id = operation["requestId"]
        envelope, payload_hash = self._seal(session, operation)
        wait_ms = min(120_000, timeout_ms + 10_000)
        queue_deadline = time.monotonic() + (queue_ms / 1000.0)

        while True:
            response = self._request(
                "POST",
                f"/v1/sessions/{session.session_id}/operations",
                headers=self._auth(session),
                json={
                    "requestId": request_id,
                    "envelope": envelope,
                    "payloadHash": payload_hash,
                    "timeoutMs": timeout_ms,
                    "waitMs": wait_ms,
                },
            )
            if response.status_code == 409:
                body = _json_or_empty(response)
                error = body.get("error")
                if error == "busy":
                    if time.monotonic() < queue_deadline:
                        time.sleep(BUSY_RETRY_SECONDS)
                        continue
                    raise SessionBusyError("session_busy", "another operation is running", 409, request_id)
                if error == "duplicate":
                    existing = self.get_operation(session, request_id, wait_ms)
                    if existing is not None:
                        return existing
                raise self._error_from(response, "exec_failed")
            if response.status_code == 202:
                pending = self.get_operation(session, request_id, wait_ms)
                if pending is not None:
                    return pending
                raise SandboxError(
                    "operation_pending",
                    "the operation is still running; fetch it by request id",
                    202,
                    request_id,
                )
            if response.status_code != 200:
                raise self._error_from(response, "exec_failed")
            body = _json_or_empty(response)
            envelope_out = body.get("envelope")
            if not envelope_out:
                raise SandboxError("missing_result", "server returned no result envelope", 200, request_id)
            return self._open(session, envelope_out)

    def get_operation(
        self, session: SandboxSession, request_id: str, wait_ms: int = 20_000
    ) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"/v1/sessions/{session.session_id}/requests/{request_id}",
            headers=self._auth(session),
            params={"waitMs": wait_ms},
        )
        if response.status_code == 202:
            return None
        if response.status_code == 409:
            body = _json_or_empty(response)
            if body.get("error") == "outcome_unknown":
                raise OutcomeUnknownError(
                    "outcome_unknown",
                    "the controller restarted with this request in flight; the command may have run",
                    409,
                    request_id,
                )
            raise SandboxError(body.get("error", "lookup_failed"), "operation lookup failed", 409, request_id)
        if response.status_code == 410:
            raise ResultExpiredError("result_expired", "the result is no longer available", 410, request_id)
        if response.status_code == 404:
            raise SandboxError("request_not_found", "the controller never saw this request id", 404, request_id)
        if response.status_code != 200:
            raise self._error_from(response, "operation_lookup_failed")
        body = _json_or_empty(response)
        envelope = body.get("envelope")
        if not envelope:
            return None
        return self._open(session, envelope)

    def cancel(self, session: SandboxSession, request_id: str) -> None:
        response = self._request(
            "DELETE",
            f"/v1/sessions/{session.session_id}/requests/{request_id}",
            headers=self._auth(session),
        )
        if response.status_code in (200, 202, 204, 404, 410):
            return
        raise self._error_from(response, "cancel_failed")

    # ------------------------------------------------------------------ exec

    def exec(
        self,
        session: SandboxSession,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout_ms: int = 60_000,
        queue_ms: int = 0,
        request_id: str | None = None,
    ) -> ExecResult:
        operation: dict[str, Any] = {
            "v": 2,
            "sessionId": session.session_id,
            "clientNonce": session.client_nonce,
            "requestId": request_id or os.urandom(16).hex(),
            "op": "exec",
            "command": command,
        }
        if env:
            operation["env"] = env
        if cwd:
            operation["cwd"] = cwd
        if stdin is not None:
            operation["stdin"] = stdin
        if timeout_ms:
            operation["timeoutMs"] = timeout_ms
        raw = self._operation(session, operation, timeout_ms=timeout_ms, queue_ms=queue_ms)
        return ExecResult(
            ok=bool(raw.get("ok")),
            exit_code=raw.get("exitCode"),
            stdout=raw.get("stdout") or "",
            stderr=raw.get("stderr") or "",
            timed_out=bool(raw.get("timedOut")),
            cancelled=bool(raw.get("cancelled")),
            duration_ms=raw.get("durationMs"),
            truncated_stdout=bool((raw.get("truncated") or {}).get("stdout")),
            truncated_stderr=bool((raw.get("truncated") or {}).get("stderr")),
            error_code=(raw.get("error") or {}).get("code"),
            error_message=(raw.get("error") or {}).get("message"),
            request_id=operation["requestId"],
        )

    # ----------------------------------------------------------------- files

    def put_file(
        self,
        session: SandboxSession,
        path: str,
        data: bytes,
        *,
        archive: bool = False,
        mode: int | None = None,
    ) -> dict[str, Any]:
        if len(data) > session.limits.max_file_bytes:
            raise SandboxError(
                "file_too_large",
                f"{len(data)} bytes exceeds this session's {session.limits.max_file_bytes} byte limit",
            )
        operation: dict[str, Any] = {
            "v": 2,
            "sessionId": session.session_id,
            "clientNonce": session.client_nonce,
            "requestId": os.urandom(16).hex(),
            "op": "file.put",
            "path": path,
            "dataBase64": base64.b64encode(data).decode("ascii"),
        }
        if archive:
            operation["archive"] = True
        if mode is not None:
            operation["mode"] = mode
        result = self._operation(session, operation, timeout_ms=30_000, queue_ms=0)
        _raise_on_guest_error(result)
        return result

    def get_file(self, session: SandboxSession, path: str, *, archive: bool = False) -> bytes:
        operation = {
            "v": 2,
            "sessionId": session.session_id,
            "clientNonce": session.client_nonce,
            "requestId": os.urandom(16).hex(),
            "op": "file.get",
            "path": path,
            "archive": archive,
        }
        result = self._operation(session, operation, timeout_ms=60_000, queue_ms=0)
        _raise_on_guest_error(result)
        return base64.b64decode(result.get("dataBase64") or "")

    def list_files(self, session: SandboxSession, path: str = "/workspace", *, recursive: bool = False):
        operation = {
            "v": 2,
            "sessionId": session.session_id,
            "clientNonce": session.client_nonce,
            "requestId": os.urandom(16).hex(),
            "op": "file.list",
            "path": path,
            "recursive": recursive,
        }
        result = self._operation(session, operation, timeout_ms=60_000, queue_ms=0)
        _raise_on_guest_error(result)
        return result.get("entries") or []

    def stat_file(self, session: SandboxSession, path: str) -> dict[str, Any]:
        operation = {
            "v": 2,
            "sessionId": session.session_id,
            "clientNonce": session.client_nonce,
            "requestId": os.urandom(16).hex(),
            "op": "file.stat",
            "path": path,
        }
        result = self._operation(session, operation, timeout_ms=30_000, queue_ms=0)
        _raise_on_guest_error(result)
        return result.get("entry") or {}


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _raise_on_guest_error(result: dict[str, Any]) -> None:
    if result.get("ok"):
        return
    error = result.get("error") or {}
    raise SandboxError(error.get("code") or "guest_error", error.get("message") or "guest reported an error")


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return min(2.0**attempt, 30.0)
