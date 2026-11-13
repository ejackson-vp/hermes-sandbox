"""Throwaway smoke check: create, exec, destroy against a real API.

Proves the parts I cannot derive from source alone: the mock payment payload, the
client commitment signature, and the sealed envelope round trip.
"""

from __future__ import annotations

import os
import sys
import time

from hermes_sandbox.client import SandboxClient
from hermes_sandbox.errors import SandboxError

API_URL = os.environ.get("SANDBOX_API_URL", "https://sandbox-api.taila53c50.ts.net")


def main() -> int:
    started = time.time()
    with SandboxClient(API_URL, timeout_seconds=90) as client:
        images = client.list_images()
        print(f"images: {[i.get('id') for i in images]}")

        session = client.create_session(tier="standard")
        print(f"created {session.session_id} state={session.state} limits={session.limits}")

        client.wait_for_ready(session, timeout_seconds=90)
        print(f"ready in {time.time() - started:.2f}s guest_key={'yes' if session.guest_public_key else 'no'}")
        print(f"remaining={session.remaining_seconds()}s expires={session.lease_expires_at}")

        result = client.exec(session, ["bash", "-c", "uname -sr; echo host=$(hostname); id -u"])
        print(f"exec ok={result.ok} exit={result.exit_code} duration={result.duration_ms}ms")
        print("--- stdout ---")
        print(result.stdout.strip())

        failed = client.exec(session, ["bash", "-c", "echo out; echo err >&2; exit 42"])
        print(f"nonzero ok={failed.ok} exit={failed.exit_code} out={failed.stdout.strip()!r} err={failed.stderr.strip()!r}")

        client.put_file(session, "/workspace/hello.txt", b"sealed hello\n")
        data = client.get_file(session, "/workspace/hello.txt")
        print(f"file roundtrip: {data!r}")

        entries = client.list_files(session, "/workspace")
        print(f"listing: {[e.get('path') for e in entries]}")

        client.destroy(session)
        print(f"destroyed {session.session_id} in {time.time() - started:.2f}s total")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SandboxError as exc:
        print(f"FAILED {type(exc).__name__}: {exc} (status={exc.status})", file=sys.stderr)
        sys.exit(1)
